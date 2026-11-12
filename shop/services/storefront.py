import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from common import log

CART = os.getenv("CART_URL", "http://cart:8000")
PAYMENT = os.getenv("PAYMENT_URL", "http://payment:8000")
app = FastAPI(title="storefront")


class Add(BaseModel):
    user_id: str = "guest"
    product_id: int
    qty: int = 1


def call(method, url, **kw):
    try:
        r = httpx.request(method, url, timeout=3, **kw)
    except Exception as e:
        log("ERROR", f"{method} {url} unreachable: {e}")
        raise HTTPException(502, f"upstream unreachable: {url}")
    if r.status_code >= 400:
        log("ERROR", f"{method} {url} returned {r.status_code}: {r.text[:150]}")
        raise HTTPException(r.status_code, r.json().get("detail", "upstream error"))
    return r.json()


@app.get("/health")
def health():
    # The storefront itself is healthy if it can answer. Dependency state is reported, not inherited,
    # so the agent can tell a broken storefront apart from a broken dependency.
    deps = {}
    for name, url in (("cart", CART), ("payment", PAYMENT)):
        try:
            deps[name] = httpx.get(f"{url}/health", timeout=2).status_code
        except Exception:
            deps[name] = "unreachable"
    return {"service": "storefront", "status": "ok", "dependencies": deps}


@app.get("/api/products")
def products():
    return call("GET", f"{CART}/products")


@app.post("/api/cart/add")
def add(a: Add):
    return call("POST", f"{CART}/cart/{a.user_id}/add", json={"product_id": a.product_id, "qty": a.qty})


@app.get("/api/cart/{user_id}")
def cart(user_id: str):
    return call("GET", f"{CART}/cart/{user_id}")


@app.post("/api/checkout/{user_id}")
def checkout(user_id: str):
    return call("POST", f"{PAYMENT}/pay", json={"user_id": user_id})


@app.post("/api/probe")
def probe():
    """Synthetic customer journey used by Monitoring: add one item, then pay."""
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
