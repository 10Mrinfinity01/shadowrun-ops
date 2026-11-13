"""Recurring problems: when the same problem keeps coming back, fixing the symptom again isn't enough.

Rule: if the same service has been the root cause RECURRENCE_THRESHOLD times within RECURRENCE_WINDOW_S
(counting the current incident), switch to the REVERSE strategy: roll back the most recent human change to
that service (release or config) to its known-good version. If there is no such change to reverse, the
normal fix is applied again and the engineer is told that a permanent fix is needed.
"""
import time

from agent import changes
from agent.contracts import Diagnosis, Hypothesis
from agent.planner.planner import plan

RECURRENCE_WINDOW_S = 600
RECURRENCE_THRESHOLD = 3
CHANGE_LOOKBACK_S = 3600


def count_recent(history, service, now=None, window_s=RECURRENCE_WINDOW_S):
    """How many earlier incidents in the window had this service as the root cause."""
    now = now or time.time()
    n = 0
    for h in history:
        cause = ((h.get("diagnosis") or {}).get("root_cause") or {})
        if cause.get("service") == service and now - h.get("started", 0) <= window_s:
            n += 1
    return n


def reverse_proposal(diagnosis, known_good, now=None):
    """Plan a rollback of the latest human change to the root-cause service, if one is still running."""
    now = now or time.time()
    cause = diagnosis.root_cause
    if cause is None:
        return None, None
    candidates = [c for c in changes.all_changes()
                  if c["service"] == cause.service and not c.get("is_rollback") and not c.get("is_reset")
                  and now - c["time"] <= CHANGE_LOOKBACK_S
                  and changes.read_runtime(c["service"], c["kind"]) == c["after"]]
    if not candidates:
        return None, None
    c = candidates[-1]
    kind = "bad_deploy" if c["kind"] == "release" else "bad_config"
    hyp = Hypothesis(id=f"{kind}:{cause.service}", kind=kind, service=cause.service, container=cause.container,
                     change_id=c["id"], score=diagnosis.confidence,
                     supporting=[f"recurring problem: reversing {c['id']} by {c['actor']}"])
    reversed_diag = Diagnosis(incident_id=diagnosis.incident_id, root_cause=hyp, confidence=diagnosis.confidence,
                              explanation=diagnosis.explanation)
    return plan(reversed_diag, known_good), c
