"""The team's deploy tool. Every release and config change goes through here and is recorded in the ledger.

Usage (repo root, Laptop A):
    python shop/deploy.py release payment 2.3 --by dev-ravi
    python shop/deploy.py config payment db_port=5433 --by dev-ravi
    python shop/deploy.py status
"""
import argparse
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from agent import changes  # noqa: E402

CONTAINERS = {"cart": "shop-cart", "payment": "shop-payment", "storefront": "shop-storefront"}


def restart(service):
    r = subprocess.run(["docker", "restart", CONTAINERS[service]], capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip())


def parse_value(v):
    try:
        return int(v)
    except ValueError:
        return v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["release", "config", "status"])
    ap.add_argument("service", nargs="?", choices=list(CONTAINERS))
    ap.add_argument("values", nargs="*")
    ap.add_argument("--by", default="dev-team")
    ap.add_argument("--note", default="")
    a = ap.parse_args()

    if a.action == "status":
        for s in CONTAINERS:
            print(f"{s:<11} release={changes.read_runtime(s, 'release')['version']:<5} "
                  f"config={changes.read_runtime(s, 'config')}")
        return

    if not a.service or not a.values:
        ap.error("service and value(s) required")
    kind = a.action
    before = changes.read_runtime(a.service, kind)
    if kind == "release":
        after = {**before, "version": a.values[0]}
    else:
        after = dict(before)
        for kv in a.values:
            k, _, v = kv.partition("=")
            after[k] = parse_value(v)
    entry = changes.record_change(a.service, kind, before, after, actor=a.by, note=a.note)
    changes.write_runtime(a.service, kind, after)
    restart(a.service)
    print(f"Deployed {a.service} {kind}: {before} -> {after}  ({entry['id']} by {a.by})")


if __name__ == "__main__":
    main()
