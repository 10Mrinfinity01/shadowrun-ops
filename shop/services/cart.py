from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import connect, db_ok, init_schema_in_background, log

app = FastAPI(title="cart")

init_schema_in_background([
    """CREATE TABLE IF NOT EXISTS products (
        id SERIAL PRIMARY KEY, name TEXT NOT NULL, price_paise INT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS cart_items (
        user_id TEXT NOT NULL, product_id INT NOT NULL REFERENCES products(id),
        qty INT NOT NULL, PRIMARY KEY (user_id, product_id))""",
    """INSERT INTO products (name, price_paise)
       SELECT * FROM (VALUES ('Masala chai pack', 24900), ('Steel water bottle', 59900),
                             ('Cotton tote bag', 19900), ('Notebook set', 34900)) AS v(name, price)
       WHERE NOT EXISTS (SELECT 1 FROM products)""",
])


class AddItem(BaseModel):
    product_id: int
    qty: int = 1


@app.get("/health")
def health():
    ok, reason = db_ok()
    body = {"service": "cart", "status": "ok" if ok else "fail", "checks": {"database": reason}}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.get("/products")
def products():
    try:
        with connect() as conn:
            rows = conn.execute("SELECT id, name, price_paise FROM products ORDER BY id").fetchall()
        return [{"id": r[0], "name": r[1], "price_paise": r[2]} for r in rows]
    except Exception as e:
        log("ERROR", f"GET /products failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.post("/cart/{user_id}/add")
def add(user_id: str, item: AddItem):
    try:
        with connect() as conn:
            conn.execute(
                """INSERT INTO cart_items (user_id, product_id, qty) VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, product_id) DO UPDATE SET qty = cart_items.qty + EXCLUDED.qty""",
                (user_id, item.product_id, item.qty),
            )
        return {"ok": True}
    except Exception as e:
        log("ERROR", f"POST /cart/{user_id}/add failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.get("/cart/{user_id}")
def get_cart(user_id: str):
    try:
        with connect() as conn:
            rows = conn.execute(
                """SELECT p.id, p.name, p.price_paise, c.qty FROM cart_items c
                   JOIN products p ON p.id = c.product_id WHERE c.user_id = %s ORDER BY p.id""",
                (user_id,),
            ).fetchall()
        items = [{"id": r[0], "name": r[1], "price_paise": r[2], "qty": r[3]} for r in rows]
        return {"items": items, "total_paise": sum(i["price_paise"] * i["qty"] for i in items)}
    except Exception as e:
        log("ERROR", f"GET /cart/{user_id} failed: {e}")
        raise HTTPException(503, "database unavailable")
