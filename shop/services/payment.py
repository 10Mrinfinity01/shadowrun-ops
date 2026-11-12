from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from common import connect, db_ok, init_schema_in_background, log

app = FastAPI(title="payment")

init_schema_in_background([
    """CREATE TABLE IF NOT EXISTS orders (
        id SERIAL PRIMARY KEY, user_id TEXT NOT NULL, amount_paise INT NOT NULL,
        status TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())""",
])


class PayRequest(BaseModel):
    user_id: str


@app.get("/health")
def health():
    ok, reason = db_ok()
    body = {"service": "payment", "status": "ok" if ok else "fail", "checks": {"database": reason}}
    return JSONResponse(body, status_code=200 if ok else 503)


@app.post("/pay")
def pay(req: PayRequest):
    """Simulated payment: no real gateway. Charges the cart total and records an order in one transaction."""
    try:
        with connect() as conn:
            with conn.transaction():
                row = conn.execute(
                    """SELECT COALESCE(SUM(p.price_paise * c.qty), 0) FROM cart_items c
                       JOIN products p ON p.id = c.product_id WHERE c.user_id = %s""",
                    (req.user_id,),
                ).fetchone()
                total = row[0]
                if total == 0:
                    raise HTTPException(400, "cart is empty")
                order_id = conn.execute(
                    "INSERT INTO orders (user_id, amount_paise, status) VALUES (%s, %s, 'paid') RETURNING id",
                    (req.user_id, total),
                ).fetchone()[0]
                conn.execute("DELETE FROM cart_items WHERE user_id = %s", (req.user_id,))
        return {"ok": True, "order_id": order_id, "amount_paise": total}
    except HTTPException:
        raise
    except Exception as e:
        log("ERROR", f"POST /pay failed for user={req.user_id}: {e}")
        raise HTTPException(503, "payment failed: database unavailable")
