"""Change ledger + known-good store.

- Ledger: every deploy and config change, who made it and when (data/change_ledger.jsonl).
- Known-good store: the last release/config of each service that ran healthily (data/known_good.json).
- Runtime files: data/runtime/<service>.<release|config>.json, mounted read-only into the containers.
"""
import json
import pathlib
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
RUNTIME = DATA / "runtime"
LEDGER = DATA / "change_ledger.jsonl"
KNOWN_GOOD = DATA / "known_good.json"

MANAGED = ["cart", "payment", "storefront"]
DEFAULTS = {
    "release": {"version": "2.2"},
    "config": {"db_host": "demo-db", "db_port": 5432, "db_timeout_s": 2},
}
DEPLOY_WINDOW_S = 12        # monitoring stays quiet while a deploy restarts the container
PROMOTE_AFTER_HEALTHY_S = 60


def _ensure():
    RUNTIME.mkdir(parents=True, exist_ok=True)


def read_runtime(service, kind):
    path = RUNTIME / f"{service}.{kind}.json"
    try:
        return {**DEFAULTS[kind], **json.loads(path.read_text())}
    except FileNotFoundError:
        return dict(DEFAULTS[kind])


def write_runtime(service, kind, value):
    _ensure()
    (RUNTIME / f"{service}.{kind}.json").write_text(json.dumps(value, indent=2))


def record_change(service, kind, before, after, actor, note="", is_rollback=False, is_reset=False):
    _ensure()
    now = time.time()
    entry = {"id": "CHG-" + uuid.uuid4().hex[:6].upper(), "time": now, "service": service, "kind": kind,
             "before": before, "after": after, "actor": actor, "note": note,
             "is_rollback": is_rollback, "is_reset": is_reset, "deploy_window_until": now + DEPLOY_WINDOW_S}
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def all_changes():
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def recent_changes(window_s=600, now=None):
    """Changes that could explain an incident: recent, made by people, not rollbacks or demo resets."""
    now = now or time.time()
    return [c for c in all_changes()
            if now - c["time"] <= window_s and not c.get("is_rollback") and not c.get("is_reset")]


def in_deploy_window(now=None):
    now = now or time.time()
    return any(c.get("deploy_window_until", 0) > now for c in all_changes()[-10:])


class KnownGood:
    def __init__(self, path=None):
        self._path = pathlib.Path(path) if path else None
        self.promoted = set()

    @property
    def path(self):
        return self._path or KNOWN_GOOD

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {s: {k: dict(v) for k, v in DEFAULTS.items()} for s in MANAGED}

    def get(self, service, kind):
        return self.load().get(service, {}).get(kind, dict(DEFAULTS[kind]))

    def set(self, service, kind, value):
        data = self.load()
        data.setdefault(service, {})[kind] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))

    def promote_stable(self, last_unhealthy_at, now=None):
        """A change becomes known-good after running PROMOTE_AFTER_HEALTHY_S with no trouble since it was made."""
        now = now or time.time()
        promoted = []
        for c in all_changes():
            if c["id"] in self.promoted or c.get("is_reset"):
                continue
            if now - c["time"] >= PROMOTE_AFTER_HEALTHY_S and last_unhealthy_at < c["time"]:
                if read_runtime(c["service"], c["kind"]) == c["after"]:  # still what is running
                    self.set(c["service"], c["kind"], c["after"])
                    promoted.append(c)
                self.promoted.add(c["id"])
            elif last_unhealthy_at >= c["time"]:
                self.promoted.add(c["id"])  # trouble happened after this change: never promote it
        return promoted
