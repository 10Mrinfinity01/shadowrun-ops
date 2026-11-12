"""Fake customers for QuickCart. Keeps the shop busy so outages have a real, countable cost.

Each "customer" adds a random product to their cart and pays. Every failed checkout is counted
as lost revenue. Outages are tracked as episodes (first failure -> first success again).
Stats are written to data/traffic_stats.json for the agent's dashboard.

Run in its own window (repo root, Laptop A):
    .\\venv\\Scripts\\python.exe traffic\\traffic.py            # about 2 customers per second
    .\\venv\\Scripts\\python.exe traffic\\traffic.py --rate 4
"""
import argparse
import json
import os
import pathlib
import random
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
STATS = ROOT / "data" / "traffic_stats.json"
SHOP = "http://localhost:8080"
FALLBACK_PRICES = {1: 24900, 2: 59900, 3: 19900, 4: 34900}


def call(method, path, body=None, timeout=3):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(SHOP + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def write_stats(stats):
    STATS.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATS.with_suffix(".tmp")
    tmp.write_text(json.dumps(stats))
    os.replace(tmp, STATS)  # atomic: the agent never reads a half-written file


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=2.0, help="customers per second")
    rate = ap.parse_args().rate

    prices = dict(FALLBACK_PRICES)
    try:
        prices = {p["id"]: p["price_paise"] for p in call("GET", "/api/products")}
    except Exception:
        pass

    stats = {"started": time.time(), "updated": time.time(), "ok": 0, "failed": 0,
             "revenue_paise": 0, "lost_paise": 0, "current_outage": None, "outages": []}
    print(f"Fake customers running at {rate}/s against {SHOP}. Ctrl + C to stop.")
    n = 0
    while True:
        n += 1
        user = f"cust-{random.randint(1000, 9999)}"
        pid = random.choice(list(prices))
        qty = random.choice([1, 1, 1, 2])
        value = prices[pid] * qty
        try:
            call("POST", "/api/cart/add", {"user_id": user, "product_id": pid, "qty": qty})
            call("POST", f"/api/checkout/{user}")
            stats["ok"] += 1
            stats["revenue_paise"] += value
            if stats["current_outage"]:
                ep = stats["current_outage"]
                ep["ended"] = time.time()
                stats["outages"] = ([ep] + stats["outages"])[:20]
                stats["current_outage"] = None
                print(f"  recovered: {ep['failed']} checkouts failed, Rs {ep['lost_paise'] / 100:.0f} lost")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ConnectionError, OSError):
            stats["failed"] += 1
            stats["lost_paise"] += value
            ep = stats["current_outage"] or {"started": time.time(), "ended": None, "failed": 0, "lost_paise": 0}
            ep["failed"] += 1
            ep["lost_paise"] += value
            stats["current_outage"] = ep
            if ep["failed"] == 1:
                print("  checkout failing: outage started")
        stats["updated"] = time.time()
        write_stats(stats)
        if n % 20 == 0:
            print(f"  {stats['ok']} ok / {stats['failed']} failed, Rs {stats['revenue_paise'] / 100:.0f} earned, "
                  f"Rs {stats['lost_paise'] / 100:.0f} lost")
        time.sleep(max(0.05, 1 / rate))


if __name__ == "__main__":
    main()
