"""Incident memory: problems the agent has already solved. A repeat problem costs zero AI tokens.

Each solved incident is stored under a FINGERPRINT made of what was observed (not what was injected):
  - which checks failed            e.g. customer_probe:checkout
  - the error type in the logs     e.g. payment:AttributeError
  - which recent changes exist     e.g. release:payment
The fix that worked is remembered with it. Next time the same fingerprint shows up, the agent skips the
AI investigation, but still checks the remembered cause against today's evidence, practises the fix on
the shadow copy and verifies it, because memory can be wrong.
"""
import hashlib
import json
import re
import time
from collections import Counter

from agent import changes, config

MEMORY_FILE = changes.DATA / "memory.json"
ERROR_TYPE = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Timeout))\b|(connection refused|timed out|no such host)",
                        re.I)


def error_signature(lines):
    """Most common error type in ERROR/WARN log lines, or None."""
    found = Counter()
    for line in lines:
        if " ERROR " not in f" {line} " and " WARN " not in f" {line} ":
            continue
        matches = list(ERROR_TYPE.finditer(line))
        if matches:  # the last one is the real exception: "PaymentError ...: AttributeError: ..."
            m = matches[-1]
            found[(m.group(1) or m.group(2)).lower()] += 1
    return found.most_common(1)[0][0] if found else None


def fingerprint(event, recent_changes, read_logs):
    signals = sorted({f"{s['type']}:{s['target']}" for s in event.get("signals", []) if s.get("result") == "failed"})
    look_at = list(dict.fromkeys(list(event.get("affected_services", [])) + [c["service"] for c in recent_changes]))
    errors = []
    for name in look_at:
        svc = config.SERVICES.get(name)
        if svc:
            sig = error_signature(read_logs(svc["container"])[-40:])
            if sig:
                errors.append(f"{name}:{sig}")
    change_kinds = sorted({f"{c['kind']}:{c['service']}" for c in recent_changes})
    readable = f"failed={','.join(signals) or '-'} | errors={','.join(sorted(errors)) or '-'} | changes={','.join(change_kinds) or '-'}"
    return hashlib.sha1(readable.encode()).hexdigest()[:10], readable


class IncidentMemory:
    def __init__(self, path=None):
        self.path = path or MEMORY_FILE
        self.items = self._load()

    def _load(self):
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.items, indent=2))
        except Exception as e:
            print("could not save memory:", e)

    def recall(self, fp):
        item = self.items.get(fp)
        return item if item and item.get("times_fixed", 0) >= 1 else None

    def remember(self, fp, readable, cause, operation, incident_id, recovery_s, tokens_used):
        item = self.items.get(fp) or {"readable": readable, "times_fixed": 0, "first_tokens": tokens_used,
                                      "first_incident": incident_id}
        item.update({"cause": {"kind": cause["kind"], "service": cause["service"]}, "operation": operation,
                     "last_incident": incident_id, "last_time": time.time(), "last_recovery_s": recovery_s})
        item["times_fixed"] += 1
        self.items[fp] = item
        self._save()
        return item

    def forget(self, fp):
        """A remembered fix that failed: drop it so the agent investigates properly next time."""
        if self.items.pop(fp, None) is not None:
            self._save()

    def clear(self):
        self.items = {}
        self._save()

    def stats(self):
        return {"problems_remembered": len(self.items),
                "entries": [{"fingerprint": fp, "readable": v["readable"], "cause": v["cause"],
                             "operation": v["operation"], "times_fixed": v["times_fixed"]}
                            for fp, v in sorted(self.items.items(), key=lambda kv: -kv[1].get("last_time", 0))][:8]}
