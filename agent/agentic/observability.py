"""Observability: the always-on sensors. Plain code, no AI, zero tokens.

Every monitor tick is recorded as one data point: per-service health, container state and error lines
logged since the last tick, plus the customer checkout result/latency and the money-vs-orders check.
From that history it provides:
  - metrics(service):  uptime, error rate and latency over the last minute
  - alerts():          named alert rules that are firing right now (like Prometheus alert rules)
  - service_map():     the dependency graph coloured by health, with the blast radius of a failure
  - series():          short history for the dashboard sparklines
"""
import time
from collections import deque

from agent import config

ALERT_RULES = [
    {"name": "CheckoutFailing", "severity": "critical", "about": "customer checkout failed on the last 2 checks"},
    {"name": "CheckoutSlow", "severity": "high", "about": f"checkout slower than {config.PROBE_SLOW_MS} ms"},
    {"name": "ServiceDown", "severity": "critical", "about": "a container is stopped, missing or out of memory"},
    {"name": "ServiceFrozen", "severity": "critical", "about": "a container is paused: running but not answering"},
    {"name": "HealthCheckFailing", "severity": "high", "about": "a service fails its health check"},
    {"name": "PaymentOrderMismatch", "severity": "critical", "about": "money taken without a matching paid order"},
    {"name": "ErrorSpike", "severity": "high", "about": "a service logged 5+ errors in the last 15 seconds"},
]
ERROR_SPIKE_LINES = 5
ERROR_SPIKE_WINDOW_S = 15


def dependents_of(service):
    return [s for s, deps in config.DEPENDS_ON.items() if service in deps]


def blast_radius(failing):
    """Everything that depends (directly or not) on a failing service, plus the customers."""
    hit, todo = set(), list(failing)
    while todo:
        s = todo.pop()
        for d in dependents_of(s):
            if d not in hit and d not in failing:
                hit.add(d)
                todo.append(d)
    return sorted(hit)


class Observability:
    def __init__(self, keep=200):
        self.points = deque(maxlen=keep)

    # ---- recording ---------------------------------------------------------------------------
    def record(self, snap, errors=None):
        errors = errors or {}
        self.points.append({
            "t": snap.get("time", time.time()),
            "services": {n: {"ok": s["health"] == "ok", "status": s["container_status"],
                             "oom": bool(s.get("oom_killed")), "errors": int(errors.get(n, 0))}
                         for n, s in snap["services"].items()},
            "probe": {"ok": snap["probe"]["result"] == "ok", "ms": snap["probe"].get("latency_ms"),
                      "slow": bool(snap["probe"].get("slow"))},
            "money_ok": (snap.get("consistency") or {}).get("result") != "failed",
        })

    def latest(self):
        return self.points[-1] if self.points else None

    def _window(self, seconds):
        if not self.points:
            return []
        cut = self.points[-1]["t"] - seconds
        return [p for p in self.points if p["t"] >= cut]

    # ---- metrics -----------------------------------------------------------------------------
    def metrics(self, service=None, seconds=60):
        pts = self._window(seconds)
        if not pts:
            return {"points": 0}
        if service in (None, "checkout"):
            ms = [p["probe"]["ms"] for p in pts if p["probe"]["ms"] is not None]
            return {"service": "checkout", "points": len(pts), "window_s": seconds,
                    "success_pct": round(100 * sum(p["probe"]["ok"] for p in pts) / len(pts)),
                    "latency_ms_avg": round(sum(ms) / len(ms)) if ms else None,
                    "latency_ms_last": pts[-1]["probe"]["ms"],
                    "money_mismatch_checks": sum(not p["money_ok"] for p in pts)}
        rows = [p["services"].get(service) for p in pts if service in p["services"]]
        if not rows:
            return {"service": service, "points": 0, "detail": "unknown service"}
        failed_since = None
        for p in reversed(pts):
            s = p["services"].get(service)
            if s and not s["ok"]:
                failed_since = p["t"]
            else:
                break
        return {"service": service, "points": len(rows), "window_s": seconds,
                "uptime_pct": round(100 * sum(r["ok"] for r in rows) / len(rows)),
                "errors_logged": sum(r["errors"] for r in rows),
                "errors_per_min": round(sum(r["errors"] for r in rows) * 60 / max(seconds, 1)),
                "container_now": rows[-1]["status"],
                "failing_for_s": round(pts[-1]["t"] - failed_since) if failed_since else 0}

    # ---- alert rules -------------------------------------------------------------------------
    def alerts(self):
        if not self.points:
            return []
        last = self.points[-1]
        prev = self.points[-2] if len(self.points) > 1 else last
        firing = []

        def fire(name, detail, targets):
            rule = next(r for r in ALERT_RULES if r["name"] == name)
            firing.append({"name": name, "severity": rule["severity"], "detail": detail, "targets": targets})

        if not last["probe"]["ok"] and not prev["probe"]["ok"]:
            if last["probe"]["slow"]:
                fire("CheckoutSlow", f"checkout took {last['probe']['ms']} ms", ["checkout"])
            else:
                fire("CheckoutFailing", "customers cannot complete checkout", ["checkout"])
        down = [n for n, s in last["services"].items() if s["status"] not in ("running", "paused") or s["oom"]]
        frozen = [n for n, s in last["services"].items() if s["status"] == "paused"]
        unhealthy = [n for n, s in last["services"].items() if not s["ok"] and n not in down + frozen]
        if down:
            fire("ServiceDown", ", ".join(down) + " not running", down)
        if frozen:
            fire("ServiceFrozen", ", ".join(frozen) + " paused", frozen)
        if unhealthy:
            fire("HealthCheckFailing", ", ".join(unhealthy) + " failing health checks", unhealthy)
        if not last["money_ok"]:
            fire("PaymentOrderMismatch", "payments without a matching paid order", ["payment"])
        recent = self._window(ERROR_SPIKE_WINDOW_S)
        spiking = [n for n in last["services"]
                   if sum(p["services"].get(n, {}).get("errors", 0) for p in recent) >= ERROR_SPIKE_LINES]
        if spiking:
            fire("ErrorSpike", ", ".join(spiking) + f" logged {ERROR_SPIKE_LINES}+ errors in {ERROR_SPIKE_WINDOW_S}s", spiking)
        return firing

    # ---- service map (the observability graph) -----------------------------------------------
    def service_map(self, root=None):
        last = self.latest()
        if not last:
            return {"nodes": [], "edges": [], "blast_radius": []}
        failing = [n for n, s in last["services"].items() if not s["ok"]]
        if root and root not in failing:
            failing.append(root)  # e.g. a bad deploy: payment answers health checks but breaks checkout
        blast = blast_radius(failing)
        customers_hit = (not last["probe"]["ok"]) or bool(failing)
        nodes = [{"id": "customers", "status": "failed" if not last["probe"]["ok"] else ("warn" if customers_hit else "ok"),
                  "latency_ms": last["probe"]["ms"]}]
        for n, s in last["services"].items():
            m = self.metrics(n, 60)
            status = "failed" if n in failing else "warn" if n in blast else "ok"
            nodes.append({"id": n, "status": status, "container": s["status"], "root": n == root,
                          "errors_per_min": m.get("errors_per_min", 0), "uptime_pct": m.get("uptime_pct")})
        edges = [["customers", "storefront"]] + [[a, b] for a, deps in config.DEPENDS_ON.items() for b in deps]
        return {"nodes": nodes, "edges": edges, "failing": failing, "blast_radius": blast + (["customers"] if customers_hit else [])}

    def series(self, n=40):
        pts = list(self.points)[-n:]
        return {"latency_ms": [p["probe"]["ms"] for p in pts],
                "checkout_ok": [p["probe"]["ok"] for p in pts],
                "errors": [sum(s["errors"] for s in p["services"].values()) for p in pts]}

    def public(self, root=None):
        return {"alerts": self.alerts(), "map": self.service_map(root), "series": self.series(),
                "checkout": self.metrics("checkout", 60), "rules": ALERT_RULES}
