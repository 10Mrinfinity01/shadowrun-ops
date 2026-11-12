from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import Db, load_runtime, log

RELEASE, CONFIG = load_runtime("payment")
db = Db(CONFIG)
app = FastAPI(title="payment")

db.init_schema_in_background([
    """CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, amount_paise INT NOT NULL,
        status TEXT NOT NULL, synthetic BOOLEAN DEFAULT false, created_at TIMESTAMPTZ DEFAULT now())""",
    "ALTER TABLE orders ADD COLUMN IF NOT EXISTS synthetic BOOLEAN DEFAULT false",
])


class PayRequest(BaseModel):
    user_id: str


@app.get("/health")
def health():
    ok, reason = db.ok()
    body = {"service": "payment", "status": "ok" if ok else "fail", "version": RELEASE["version"],
            "checks": {"database": reason}}
    return JSONResponse(body, status_code=200 if ok else 503)


def tokenize_card(user_id):
    """Simulated card tokenisation. Release 2.3 shipped with a bug that breaks it."""
    if RELEASE["version"] == "2.3":
        token = None  # the refactor in 2.3 forgot to pass the card details through
        return token.upper()
    return f"tok_{user_id}"


@app.post("/pay")
def pay(req: PayRequest):
    """Simulated payment: no real gateway. Charges the cart total and records an order in one transaction."""
    try:
        token = tokenize_card(req.user_id)
    except Exception as e:
        log("ERROR", f"PaymentError release={RELEASE['version']} user={req.user_id}: {type(e).__name__}: {e}")
        raise HTTPException(500, "payment failed: internal error")
    try:
        with db.connect() as conn:
            with conn.transaction():
                total = conn.execute(
                    """SELECT COALESCE(SUM(p.price_paise * c.qty), 0) FROM cart_items c
                       JOIN products p ON p.id = c.product_id WHERE c.user_id = %s""",
                    (req.user_id,),
                ).fetchone()[0]
                if total == 0:
                    raise HTTPException(400, "cart is empty")
                order_id = conn.execute(
                    """INSERT INTO orders (user_id, amount_paise, status, synthetic)
                       VALUES (%s, %s, 'paid', %s) RETURNING id""",
                    (req.user_id, total, req.user_id == "synthetic-probe"),
                ).fetchone()[0]
                conn.execute("DELETE FROM cart_items WHERE user_id = %s", (req.user_id,))
        return {"ok": True, "order_id": order_id, "amount_paise": total, "token": token[:4] + "***"}
    except HTTPException:
        raise
    except Exception as e:
        log("ERROR", f"POST /pay failed for user={req.user_id}: {e}")
        raise HTTPException(503, "payment failed: database unavailable")
