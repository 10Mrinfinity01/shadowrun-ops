"""The investigator's tools. Every tool only READS. None of them can change the shop.

The AI investigator chooses which tool to call next, one at a time, like an engineer looking at
dashboards and logs. Results are kept short (tokens cost money) and logs are sanitized first:
secrets redacted, lines that try to give the AI orders quarantined.
"""
import json
from dataclasses import dataclass, field
from typing import Callable

from agent import config
from agent.sanitizer.sanitizer import sanitize_logs

MAX_RESULT_CHARS = 700

TOOL_SPECS = {
    "service_map": {"args": {}, "about": "every service with its health, container state, errors per minute, "
                                         "and which services are only affected because something they depend on broke"},
    "metrics": {"args": {"service": "storefront|cart|payment|database|redis|checkout"},
                "about": "last minute of numbers for one service: uptime %, errors logged, how long it has been failing; "
                         "'checkout' gives customer success % and latency"},
    "logs": {"args": {"service": "storefront|cart|payment"}, "about": "the latest log lines of one service (sanitized)"},
    "deploy_history": {"args": {}, "about": "recent releases and config changes: who changed what, when, and whether "
                                            "each change is still running"},
    "container": {"args": {"service": "storefront|cart|payment|database|redis"},
                  "about": "container state: running / exited / paused / OOM-killed, exit code"},
    "money_check": {"args": {}, "about": "does every payment taken have a matching paid order?"},
    "test_checkout": {"args": {}, "about": "place one synthetic order right now and report the result and latency"},
}


@dataclass
class ToolDeps:
    obs: object                                   # Observability
    read_logs: Callable[[str], list]              # container -> lines
    effective_changes: Callable[[], list]         # changes still in effect
    all_changes: Callable[[], list]               # every ledger entry, oldest first
    latest_snap: Callable[[], dict]               # newest monitor snapshot
    test_checkout: Callable[[], dict] = lambda: {"result": "unknown"}
    root_hint: Callable[[], str] = lambda: None


@dataclass
class Toolbox:
    deps: ToolDeps
    suspicious: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def run(self, name, args=None):
        args = args if isinstance(args, dict) else {}
        fn = getattr(self, f"tool_{name}", None)
        if name not in TOOL_SPECS or fn is None:
            result = f"unknown tool '{name}'. Available: {', '.join(TOOL_SPECS)}"
        else:
            try:
                result = fn(**{k: v for k, v in args.items() if k in TOOL_SPECS[name]["args"]})
            except TypeError:
                result = f"bad arguments for {name}: expected {TOOL_SPECS[name]['args']}"
            except Exception as e:
                result = f"{name} failed: {str(e)[:120]}"
        if not isinstance(result, str):
            result = json.dumps(result, separators=(",", ":"), default=str)
        result = result[:MAX_RESULT_CHARS]
        self.calls.append({"tool": name, "args": args, "result": result})
        return result

    # ---- the tools -----------------------------------------------------------------------------
    def tool_service_map(self):
        m = self.deps.obs.service_map(self.deps.root_hint())
        return {"services": {n["id"]: {k: n[k] for k in ("status", "container", "errors_per_min") if k in n}
                             for n in m["nodes"]},
                "failing": m.get("failing", []), "affected_only_through_dependencies": m.get("blast_radius", []),
                "depends_on": config.DEPENDS_ON}

    def tool_metrics(self, service="checkout"):
        return self.deps.obs.metrics(service, 60)

    def tool_logs(self, service="payment"):
        svc = config.SERVICES.get(service)
        if not svc:
            return f"unknown service '{service}'"
        wrapped, sus = sanitize_logs(self.deps.read_logs(svc["container"])[-10:], svc["container"])
        self.suspicious.extend(s for s in sus if s not in self.suspicious)
        return wrapped

    def tool_deploy_history(self):
        running = {c["id"] for c in self.deps.effective_changes()}
        out = []
        for c in self.deps.all_changes()[-6:]:
            what = (f"release {c['before'].get('version')} -> {c['after'].get('version')}" if c["kind"] == "release"
                    else "config " + ", ".join(f"{k}: {c['before'].get(k)} -> {v}"
                                               for k, v in c["after"].items() if c["before"].get(k) != v))
            out.append({"id": c["id"], "service": c["service"], "change": what, "by": c["actor"],
                        "seconds_ago": round(self.deps.latest_snap().get("time", c["time"]) - c["time"]),
                        "still_running": c["id"] in running})
        return out or "no changes recorded"

    def tool_container(self, service="database"):
        s = self.deps.latest_snap().get("services", {}).get(service)
        if not s:
            return f"unknown service '{service}'"
        return {k: s.get(k) for k in ("container", "container_status", "oom_killed", "exit_code", "health", "detail")}

    def tool_money_check(self):
        return self.deps.latest_snap().get("consistency") or "unavailable"

    def tool_test_checkout(self):
        return self.deps.test_checkout()
