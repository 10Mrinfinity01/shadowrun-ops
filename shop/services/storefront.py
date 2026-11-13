import os
import secrets

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from common import log

CART = os.getenv("CART_URL", "http://cart:8000")
REDIS_HOST = os.getenv("REDIS_HOST", "redis")
_redis = None


def sessions():
    """Session store (Redis). Every customer request records the session; no store, no shop."""
    global _redis
    if _redis is None:
        import redis
        _redis = redis.Redis(host=REDIS_HOST, port=6379, socket_timeout=1, socket_connect_timeout=1)
    return _redis


def touch_session(user_id):
    try:
        sessions().setex(f"session:{user_id}", 1800, "active")
    except Exception as e:
        log("ERROR", f"session store unavailable host={REDIS_HOST}:6379 reason={str(e)[:120]}")
        raise HTTPException(503, "session store unavailable")
PAYMENT = os.getenv("PAYMENT_URL", "http://payment:8000")
app = FastAPI(title="storefront")


class Add(BaseModel):
    user_id: str = "guest"
    product_id: int
    qty: int = 1


class SetQty(BaseModel):
    user_id: str = "guest"
    product_id: int
    qty: int


class Register(BaseModel):
    name: str
    phone: str
    password: str


class Login(BaseModel):
    phone: str
    password: str


def call(method, url, **kw):
    try:
        r = httpx.request(method, url, timeout=3, **kw)
    except Exception as e:
        log("ERROR", f"{method} {url} unreachable: {e}")
        raise HTTPException(502, f"upstream unreachable: {url}")
    if r.status_code >= 400:
        log("ERROR", f"{method} {url} returned {r.status_code}: {r.text[:150]}")
        try:
            detail = r.json().get("detail", "upstream error")
        except Exception:
            detail = "upstream error"
        raise HTTPException(r.status_code, detail)
    return r.json()


@app.get("/health")
def health():
    # The storefront itself is healthy if it can answer. Dependency state is reported, not inherited,
    # so the agent can tell a broken storefront apart from a broken dependency.
    deps = {}
    for name, url in (("cart", CART), ("payment", PAYMENT)):
        try:
            deps[name] = httpx.get(f"{url}/health", timeout=0.8).status_code
        except Exception:
            deps[name] = "unreachable"
    try:
        sessions().ping()
        deps["redis"] = "ok"
    except Exception as e:
        log("ERROR", f"session store unavailable host={REDIS_HOST}:6379 reason={str(e)[:120]}")
        return JSONResponse({"service": "storefront", "status": "fail", "dependencies": deps,
                             "checks": {"redis": "unreachable"}}, status_code=503)
    return {"service": "storefront", "status": "ok", "dependencies": deps}


@app.get("/api/categories")
def categories():
    return call("GET", f"{CART}/categories")


@app.get("/api/products")
def products(category: str = ""):
    return call("GET", f"{CART}/products", params={"category": category} if category else None)


@app.post("/api/cart/set")
def set_qty(a: SetQty):
    touch_session(a.user_id)
    return call("POST", f"{CART}/cart/{a.user_id}/set", json={"product_id": a.product_id, "qty": a.qty})


def _new_session(user):
    token = secrets.token_urlsafe(24)
    try:
        sessions().setex(f"token:{token}", 7 * 24 * 3600, user["id"] + "|" + user["name"] + "|" + user["phone"])
    except Exception as e:
        log("ERROR", f"session store unavailable host={REDIS_HOST}:6379 reason={str(e)[:120]}")
        raise HTTPException(503, "session store unavailable: please try again")
    return {"token": token, "user": user}


@app.post("/api/auth/register")
def register(r: Register):
    return _new_session(call("POST", f"{CART}/users/register", json=r.model_dump()))


@app.post("/api/auth/login")
def login(r: Login):
    return _new_session(call("POST", f"{CART}/users/login", json=r.model_dump()))


@app.get("/api/auth/me")
def me(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(401, "not logged in")
    try:
        raw = sessions().get(f"token:{token}")
    except Exception as e:
        log("ERROR", f"session store unavailable host={REDIS_HOST}:6379 reason={str(e)[:120]}")
        raise HTTPException(503, "session store unavailable")
    if not raw:
        raise HTTPException(401, "session expired: please log in again")
    uid, name, phone = raw.decode().split("|", 2)
    return {"id": uid, "name": name, "phone": phone}


@app.post("/api/auth/logout")
def logout(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    try:
        if token:
            sessions().delete(f"token:{token}")
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/cart/add")
def add(a: Add):
    touch_session(a.user_id)
    return call("POST", f"{CART}/cart/{a.user_id}/add", json={"product_id": a.product_id, "qty": a.qty})


@app.get("/api/search")
def search(q: str = ""):
    return call("GET", f"{CART}/products/search", params={"q": q})


@app.get("/api/cart/{user_id}")
def cart(user_id: str):
    return call("GET", f"{CART}/cart/{user_id}")


@app.post("/api/checkout/{user_id}")
def checkout(user_id: str):
    touch_session(user_id)
    return call("POST", f"{PAYMENT}/pay", json={"user_id": user_id})


@app.get("/api/orders/{user_id}")
def order_history(user_id: str):
    return call("GET", f"{PAYMENT}/orders/{user_id}")


@app.post("/api/probe")
def probe():
    """Synthetic customer journey used by Monitoring: add one item, then pay."""
    touch_session("synthetic-probe")
    call("POST", f"{CART}/cart/synthetic-probe/add", json={"product_id": 1, "qty": 1})
    result = call("POST", f"{PAYMENT}/pay", json={"user_id": "synthetic-probe"})
    return {"ok": True, "order_id": result["order_id"]}


PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>QuickCart</title>
<style>body{font-family:system-ui;max-width:720px;margin:40px auto;padding:0 16px}
.p{display:flex;justify-content:space-between;padding:10px;border:1px solid #ddd;border-radius:8px;margin:8px 0}
button{padding:6px 12px;cursor:pointer}#msg{margin-top:16px;font-weight:600}</style></head>
<body><h1>QuickCart</h1><div id="products">Loading...</div>
<h2>Cart</h2><div id="cart"></div><button onclick="checkout()">Pay now</button><div id="msg"></div>
<script>
const U='guest', rs=p=>'Rs '+(p/100).toFixed(2);
async function j(m,u,b){const r=await fetch(u,{method:m,headers:{'Content-Type':'application/json'},body:b&&JSON.stringify(b)});
 const d=await r.json();if(!r.ok)throw new Error(d.detail||r.status);return d}
function msg(t,ok){const e=document.getElementById('msg');e.textContent=t;e.style.color=ok?'green':'crimson'}
async function load(){try{const ps=await j('GET','/api/products');
 document.getElementById('products').innerHTML=ps.map(p=>`<div class=p><span>${p.name} - ${rs(p.price_paise)}</span>
 <button onclick="add(${p.id})">Add</button></div>`).join('');showCart()}catch(e){msg('Shop error: '+e.message)}}
async function showCart(){try{const c=await j('GET','/api/cart/'+U);
 document.getElementById('cart').textContent=c.items.length?c.items.map(i=>i.qty+' x '+i.name).join(', ')+' = '+rs(c.total_paise):'Empty'}catch(e){msg('Cart error: '+e.message)}}
async function add(id){try{await j('POST','/api/cart/add',{user_id:U,product_id:id});msg('Added',true);showCart()}catch(e){msg('Add failed: '+e.message)}}
async function checkout(){try{const r=await j('POST','/api/checkout/'+U);msg('Order placed #'+r.order_id+' ('+rs(r.amount_paise)+')',true);showCart()}
 catch(e){msg('Payment failed: '+e.message)}}
load();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def home():
    return PAGE
