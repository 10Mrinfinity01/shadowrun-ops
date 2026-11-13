"""Always resolve + recurring problems use the reverse strategy.
Run from the repo root:  python -m unittest tests.test_phase7 -v
"""
import time
import unittest

from agent import changes, recurrence
from agent.contracts import Diagnosis, Hypothesis
from agent.safety.engine import SafetyEngine
from tests.test_phase1 import rehearsed
from tests.test_phase2 import TempData
from tests.test_phase2b import proposal

SECRET = "test-secret"


def incident(service, age_s):
    return {"started": time.time() - age_s, "diagnosis": {"root_cause": {"service": service}}}


def diag(service="payment", container="shop-payment", kind="service_unhealthy"):
    h = Hypothesis(id=f"{kind}:{service}", kind=kind, service=service, container=container, score=0.8)
    return Diagnosis(incident_id="SR-9", root_cause=h, confidence=0.8, explanation="x")


class Counting(unittest.TestCase):
    def test_counts_only_same_service_inside_window(self):
        hist = [incident("payment", 60), incident("payment", 300), incident("payment", 900), incident("cart", 30)]
        self.assertEqual(recurrence.count_recent(hist, "payment"), 2)   # 900 s ago is outside 10 min
        self.assertEqual(recurrence.count_recent(hist, "cart"), 1)


class Reverse(TempData):
    def test_reverses_latest_human_change(self):
        changes.record_change("payment", "release", {"version": "2.2"}, {"version": "2.7"}, "dev-ravi")
        changes.write_runtime("payment", "release", {"version": "2.7"})
        p, c = recurrence.reverse_proposal(diag(), changes.KnownGood())
        self.assertEqual(p.operation, "rollback_release")
        self.assertEqual((p.parameters["from"], p.parameters["to"]), ({"version": "2.7"}, {"version": "2.2"}))
        self.assertEqual(c["actor"], "dev-ravi")

    def test_ignores_changes_already_rolled_back(self):
        changes.record_change("payment", "release", {"version": "2.2"}, {"version": "2.7"}, "dev-ravi")
        changes.write_runtime("payment", "release", {"version": "2.2"})   # not running any more
        p, c = recurrence.reverse_proposal(diag(), changes.KnownGood())
        self.assertIsNone(p)

    def test_no_change_means_no_reverse(self):
        p, c = recurrence.reverse_proposal(diag("cart", "shop-cart", "oom_killed"), changes.KnownGood())
        self.assertIsNone(p)


class AlwaysResolve(unittest.TestCase):
    def test_no_cooldown_by_default(self):
        eng = SafetyEngine(SECRET)
        p = rehearsed(proposal("start_container", "demo-db"))
        eng.record_execution(p)
        p2 = rehearsed(proposal("start_container", "demo-db"))
        p2.incident_id = "SR-OTHER"
        self.assertEqual(eng.decide(p2).verdict, "ALLOW")

    def test_proven_broken_fix_still_blocked(self):
        self.assertEqual(SafetyEngine(SECRET).decide(rehearsed(proposal("start_container", "demo-db"), "failed")).verdict,
                         "BLOCK")


if __name__ == "__main__":
    unittest.main()
