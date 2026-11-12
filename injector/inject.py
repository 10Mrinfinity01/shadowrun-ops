"""Judge Injector (Phase 1, command line).

Breaks the shop the way real life would. It never tells the agent what it did:
the agent has to discover the problem from health checks, logs and container state.

Usage (from the repo root on Laptop A):
    python injector/inject.py stop_db     # database goes down
    python injector/inject.py reset       # bring every shop container back
    python injector/inject.py status      # show container states
"""
import subprocess
import sys

SHOP = ["demo-db", "shop-cart", "shop-payment", "shop-storefront"]


def docker(*args):
    return subprocess.run(["docker", *args], capture_output=True, text=True)


def stop_db():
    r = docker("stop", "demo-db")
    print("Injected: database stopped" if r.returncode == 0 else r.stderr)


def reset():
    r = docker("start", *SHOP)
    print("All shop containers started" if r.returncode == 0 else r.stderr)


def status():
    r = docker("ps", "-a", "--filter", "name=demo-db", "--filter", "name=shop-",
               "--format", "table {{.Names}}\t{{.Status}}")
    print(r.stdout or r.stderr)


SCENARIOS = {"stop_db": stop_db, "reset": reset, "status": status}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in SCENARIOS:
        print(__doc__)
        sys.exit(1)
    SCENARIOS[sys.argv[1]]()
