import hashlib
import secrets
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from catalog import CATEGORIES, PRODUCTS
from common import Db, load_runtime, log

RELEASE, CONFIG = load_runtime("cart")
db = Db(CONFIG)
app = FastAPI(title="cart")

def _seed():
    stmts = [
        """CREATE TABLE IF NOT EXISTS products (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, price_paise INT NOT NULL)""",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS mrp_paise INT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS category TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS emoji TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS unit TEXT",
        "ALTER TABLE products ADD COLUMN IF NOT EXISTS description TEXT",
        """CREATE TABLE IF NOT EXISTS cart_items (
            user_id TEXT NOT NULL, product_id INT NOT NULL REFERENCES products(id),
            qty INT NOT NULL, PRIMARY KEY (user_id, product_id))""",
        """CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, phone TEXT UNIQUE NOT NULL,
            salt TEXT NOT NULL, password_hash TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())""",
    ]
    for name, price, mrp, cat, emoji, unit, desc in PRODUCTS:
        n, d = name.replace("'", "''"), desc.replace("'", "''")
        stmts.append(f"""INSERT INTO products (name, price_paise) SELECT '{n}', {price}
                         WHERE NOT EXISTS (SELECT 1 FROM products WHERE name = '{n}')""")
        stmts.append(f"""UPDATE products SET price_paise = {price}, mrp_paise = {mrp}, category = '{cat}',
                         emoji = '{emoji}', unit = '{unit}', description = '{d}' WHERE name = '{n}'""")
    return stmts


db.init_schema_in_background(_seed())

VERSION = RELEASE["version"]


def slow_query_bug(endpoint):
    """BUG in release 2.6: a new 'recommended items' query scans the whole table on every cart call."""
    if VERSION == "2.6":
        started = time.time()
        time.sleep(3.5)
        log("WARN", f"slow query {endpoint}: {int((time.time() - started) * 1000)} ms "
                    "(release 2.6 recommendations query, full table scan on cart_items)")


_report_cache = []  # BUG (on purpose, for the memory-overload scenario): reports are kept forever


class AddItem(BaseModel):
    product_id: int
    qty: int = 1


class SetItem(BaseModel):
    product_id: int
    qty: int


class Register(BaseModel):
    name: str
    phone: str
    password: str


class Login(BaseModel):
    phone: str
    password: str


PRODUCT_COLS = "id, name, price_paise, mrp_paise, category, emoji, unit, description"


def product_row(r):
    return {"id": r[0], "name": r[1], "price_paise": r[2], "mrp_paise": r[3] or r[2], "category": r[4],
            "emoji": r[5] or "🛒", "unit": r[6] or "", "description": r[7] or ""}


def hash_pw(salt, password):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()


@app.get("/health")
def health():
    ok, reason = db.ok()
    body = {"service": "cart", "status": "ok" if ok else "fail", "version": RELEASE["version"],
            "checks": {"database": reason}}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/categories")
def categories():
    return [{"id": c, "name": n, "emoji": e} for c, n, e in CATEGORIES]


@app.get("/products")
def products(category: str = ""):
    try:
        with db.connect() as conn:
            if category:
                rows = conn.execute(f"SELECT {PRODUCT_COLS} FROM products WHERE category = %s ORDER BY id",
                                    (category,)).fetchall()
            else:
                rows = conn.execute(f"SELECT {PRODUCT_COLS} FROM products ORDER BY id").fetchall()
        return [product_row(r) for r in rows]
    except Exception as e:
        log("ERROR", f"GET /products failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.get("/products/search")
def search(q: str = ""):
    log("INFO", f"product search q={q[:300]}")  # customer text lands in the logs, as in many real apps
    try:
        with db.connect() as conn:
            rows = conn.execute(f"SELECT {PRODUCT_COLS} FROM products WHERE name ILIKE %s OR category ILIKE %s",
                                (f"%{q}%", f"%{q}%")).fetchall()
        return [product_row(r) for r in rows]
    except Exception as e:
        log("ERROR", f"GET /products/search failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.post("/admin/export-report")
def export_report():
    _report_cache.append(b"x" * 25_000_000)  # 25 MB that is never freed
    log("INFO", f"report exported; cached reports={len(_report_cache)}")
    return {"ok": True, "cached_reports": len(_report_cache)}


@app.post("/cart/{user_id}/add")
def add(user_id: str, item: AddItem):
    slow_query_bug("POST /cart/add")
    try:
        with db.connect() as conn:
            conn.execute(
                """INSERT INTO cart_items (user_id, product_id, qty) VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, product_id) DO UPDATE SET qty = cart_items.qty + EXCLUDED.qty""",
                (user_id, item.product_id, item.qty),
            )
        return {"ok": True}
    except Exception as e:
        log("ERROR", f"POST /cart/{user_id}/add failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.post("/cart/{user_id}/set")
def set_qty(user_id: str, item: SetItem):
    """Set the exact quantity of one product. 0 removes it."""
    slow_query_bug("POST /cart/set")
    try:
        with db.connect() as conn:
            if item.qty <= 0:
                conn.execute("DELETE FROM cart_items WHERE user_id = %s AND product_id = %s",
                             (user_id, item.product_id))
            else:
                conn.execute(
                    """INSERT INTO cart_items (user_id, product_id, qty) VALUES (%s, %s, %s)
                       ON CONFLICT (user_id, product_id) DO UPDATE SET qty = EXCLUDED.qty""",
                    (user_id, item.product_id, min(item.qty, 20)))
        return {"ok": True}
    except Exception as e:
        log("ERROR", f"POST /cart/{user_id}/set failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.post("/users/register")
def register(r: Register):
    phone = "".join(ch for ch in r.phone if ch.isdigit())[-10:]
    if len(phone) != 10 or len(r.password) < 4 or not r.name.strip():
        raise HTTPException(400, "enter a name, a 10-digit phone number and a password of at least 4 characters")
    salt = secrets.token_hex(8)
    uid = "u-" + secrets.token_hex(5)
    try:
        with db.connect() as conn:
            if conn.execute("SELECT 1 FROM users WHERE phone = %s", (phone,)).fetchone():
                raise HTTPException(409, "this phone number is already registered: log in instead")
            conn.execute("INSERT INTO users (id, name, phone, salt, password_hash) VALUES (%s, %s, %s, %s, %s)",
                         (uid, r.name.strip()[:40], phone, salt, hash_pw(salt, r.password)))
        return {"id": uid, "name": r.name.strip()[:40], "phone": phone}
    except HTTPException:
        raise
    except Exception as e:
        log("ERROR", f"POST /users/register failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.post("/users/login")
def login(r: Login):
    phone = "".join(ch for ch in r.phone if ch.isdigit())[-10:]
    try:
        with db.connect() as conn:
            row = conn.execute("SELECT id, name, salt, password_hash FROM users WHERE phone = %s", (phone,)).fetchone()
    except Exception as e:
        log("ERROR", f"POST /users/login failed: {e}")
        raise HTTPException(503, "database unavailable")
    if not row or not secrets.compare_digest(hash_pw(row[2], r.password), row[3]):
        raise HTTPException(401, "wrong phone number or password")
    return {"id": row[0], "name": row[1], "phone": phone}


@app.get("/cart/{user_id}")
def get_cart(user_id: str):
    slow_query_bug("GET /cart")
    try:
        with db.connect() as conn:
            rows = conn.execute(
                """SELECT p.id, p.name, p.price_paise, c.qty, p.emoji, p.unit, p.mrp_paise FROM cart_items c
                   JOIN products p ON p.id = c.product_id WHERE c.user_id = %s ORDER BY p.id""",
                (user_id,),
            ).fetchall()
        items = [{"id": r[0], "name": r[1], "price_paise": r[2], "qty": r[3], "emoji": r[4] or "🛒",
                  "unit": r[5] or "", "mrp_paise": r[6] or r[2]} for r in rows]
        return {"items": items, "total_paise": sum(i["price_paise"] * i["qty"] for i in items)}
    except Exception as e:
        log("ERROR", f"GET /cart/{user_id} failed: {e}")
        raise HTTPException(503, "database unavailable")
