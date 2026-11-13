import json
import random
import time

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import Db, load_runtime, log

RELEASE, CONFIG = load_runtime("payment")
VERSION = RELEASE["version"]
db = Db(CONFIG)
app = FastAPI(title="payment")

db.init_schema_in_background([
    """CREATE TABLE IF NOT EXISTS payments (
        id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, amount_paise INT NOT NULL, status TEXT NOT NULL,
        order_id INT, created_at TIMESTAMPTZ DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, amount_paise INT NOT NULL,
        status TEXT NOT NULL, synthetic BOOLEAN DEFAULT false, created_at TIMESTAMPTZ DEFAULT now())""",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS synthetic BOOLEAN DEFAULT false",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS payment_id INT",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS items TEXT",
])


class PayRequest(BaseModel):
    user_id: str


@app.get("/health")
def health():
    ok, reason = db.ok()
    body = {"service": "payment", "status": "ok" if ok else "fail", "version": VERSION,
            "checks": {"database": reason}}
    return JSONResponse(body, status_code=200 if ok else 503)


def tokenize_card(user_id):
    """Simulated card tokenisation. Release 2.3 shipped with a bug that breaks it."""
    if VERSION == "2.3":
        token = None  # the refactor in 2.3 forgot to pass the card details through
        return token.upper()
    return f"tok_{user_id}"


def call_gateway(amount):
    """Simulated payment gateway: answers in 50-150 ms. We give up after gateway_timeout_ms."""
    took_ms = random.randint(50, 150)
    limit = int(CONFIG.get("gateway_timeout_ms", 2000))
    if took_ms > limit:
        time.sleep(limit / 1000)
        raise TimeoutError(f"gateway did not answer within {limit} ms")
    time.sleep(took_ms / 1000)
    return f"gw_{random.randint(100000, 999999)}"


@app.post("/pay")
def pay(req: PayRequest):
    """Simulated payment: charge the cart total at the (simulated) gateway, then create the order."""
    try:
        token = tokenize_card(req.user_id)
    except Exception as e:
        log("ERROR", f"PaymentError release={VERSION} user={req.user_id}: {type(e).__name__}: {e}")
        raise HTTPException(500, "payment failed: internal error")
    try:
        with db.connect() as conn:
            lines = conn.execute(
                """SELECT p.name, p.price_paise, c.qty, COALESCE(p.emoji, '') FROM cart_items c
                   JOIN products p ON p.id = c.product_id WHERE c.user_id = %s ORDER BY p.id""",
                (req.user_id,),
            ).fetchall()
            total = sum(r[1] * r[2] for r in lines)
            items = json.dumps([{"name": r[0], "price_paise": r[1], "qty": r[2], "emoji": r[3]} for r in lines])
            if total == 0:
                raise HTTPException(400, "cart is empty")
            try:
                call_gateway(total)
            except TimeoutError as e:
                log("ERROR", f"PaymentTimeout release={VERSION} user={req.user_id}: {e}")
                raise HTTPException(504, "payment failed: gateway timeout")
            with conn.transaction():  # the charge is recorded first, as real gateways confirm before we write the order
                payment_id = conn.execute(
                    "INSERT INTO payments (user_id, amount_paise, status) VALUES (%s, %s, 'captured') RETURNING id",
                    (req.user_id, total)).fetchone()[0]
            if VERSION == "2.4":  # BUG in 2.4: the new order writer crashes after the customer was charged
                log("ERROR", f"OrderWriteError release=2.4 payment={payment_id} user={req.user_id}: "
                             "KeyError 'shipping_address' while creating order")
                raise HTTPException(500, "payment taken but order could not be created")
            status = "pending" if VERSION == "2.5" else "paid"  # BUG in 2.5: wrong status, silently
            with conn.transaction():
                order_id = conn.execute(
                    """INSERT INTO orders (user_id, amount_paise, status, synthetic, payment_id, items)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (req.user_id, total, status, req.user_id == "synthetic-probe", payment_id, items)).fetchone()[0]
                conn.execute("UPDATE payments SET order_id = %s WHERE id = %s", (order_id, payment_id))
                conn.execute("DELETE FROM cart_items WHERE user_id = %s", (req.user_id,))
        return {"ok": True, "order_id": order_id, "amount_paise": total, "token": token[:4] + "***"}
    except HTTPException:
        raise
    except Exception as e:
        log("ERROR", f"POST /pay failed for user={req.user_id}: {e}")
        raise HTTPException(503, "payment failed: database unavailable")


@app.get("/orders/{user_id}")
def orders(user_id: str):
    try:
        with db.connect() as conn:
            rows = conn.execute("""SELECT id, amount_paise, status, created_at, items FROM orders WHERE user_id = %s
                                   ORDER BY id DESC LIMIT 20""", (user_id,)).fetchall()
        return [{"id": r[0], "amount_paise": r[1], "status": r[2], "created_at": r[3].isoformat(),
                 "items": json.loads(r[4]) if r[4] else []} for r in rows]
    except Exception as e:
        log("ERROR", f"GET /orders/{user_id} failed: {e}")
        raise HTTPException(503, "database unavailable")


@app.get("/internal/consistency")
def consistency(since: float = 0):
    """Reconciliation check used by monitoring: money taken must always match an order marked paid."""
    try:
        with db.connect() as conn:
            no_order = conn.execute(
                """SELECT COUNT(*), COALESCE(SUM(amount_paise), 0) FROM payments
                   WHERE status = 'captured' AND order_id IS NULL
                   AND created_at < now() - interval '2 seconds' AND created_at >= to_timestamp(%s)""",
                (since,)).fetchone()
            mismatch = conn.execute(
                """SELECT COUNT(*) FROM orders o JOIN payments p ON p.id = o.payment_id
                   WHERE p.status = 'captured' AND o.status <> 'paid' AND o.created_at >= to_timestamp(%s)""",
                (since,)).fetchone()[0]
        return {"payments_without_order": no_order[0], "unmatched_amount_paise": int(no_order[1]),
                "status_mismatch": mismatch, "version": VERSION}
    except Exception as e:
        raise HTTPException(503, f"database unavailable: {str(e)[:80]}")
