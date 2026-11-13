"""Phase 5a: approval mode, payment category, Redis, API slowdown, consistency check. No Docker needed.
Run from the repo root:  python -m unittest tests.test_phase5 -v
"""
import sys
import types
import unittest

from agent import changes
from agent.investigation.investigator import Context, investigate, rank_hypotheses
from agent.monitoring.monitor import Monitor
from agent.planner.planner import plan
from agent.safety.engine import SafetyEngine
from tests.test_phase1 import event_for, rehearsed, snap
from tests.test_phase2 import change

SECRET = "test-secret"


def with_redis(s, status="running"):
    s["services"]["redis"] = {"container": "shop-redis", "container_status": status, "oom_killed": False,
                              "exit_code": 0 if status != "running" else None,
                              "health": "ok" if status == "running" else "failed", "detail": f"container is {status}"}
    return s


def cons(result="ok", detail=""):
    return {"result": result, "detail": detail or ("1 new payment(s) taken with NO order" if result == "failed" else "ok")}


class ApprovalMode(unittest.TestCase):
    def test_always_ask_turns_allow_into_approval(self):
        s = snap(db="exited", cart="failed", payment="failed", probe="failed")
        p = rehearsed(plan(investigate(event_for(s), s, Context(read_logs=lambda c: []))))
        eng = SafetyEngine(SECRET)
        self.assertEqual(eng.decide(p).verdict, "ALLOW")
        d = eng.decide(p, always_ask=True)
        self.assertEqual(d.verdict, "APPROVAL_REQUIRED")
        self.assertTrue(any("always ask" in r for r in d.reasons))
        self.assertIsNone(d.grant)

    def test_always_ask_never_unblocks(self):
        s = snap(db="exited", cart="failed", payment="failed", probe="failed")
        p = rehearsed(plan(investigate(event_for(s), s, Context(read_logs=lambda c: []))), status="failed")
        self.assertEqual(SafetyEngine(SECRET).decide(p, always_ask=True).verdict, "BLOCK")


class PaymentCategory(unittest.TestCase):
    def test_payment_timeout_is_a_bad_config(self):
        s = snap(probe="failed")
        c = change(kind="config", after={**changes.DEFAULTS["config"], "gateway_timeout_ms": 20},
                   before=dict(changes.DEFAULTS["config"]))
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 7, recent_changes=[c])
        d = investigate(event_for(s), s, ctx)
        self.assertEqual(d.root_cause.id, "bad_config:payment")
        self.assertIn("gateway_timeout_ms: 2000 -> 20", d.root_cause.supporting[0])

    def test_paid_but_no_order(self):
        s = snap(probe="failed")
        s["consistency"] = cons("failed")
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 4,
                      recent_changes=[change(after={"version": "2.4"})])
        d = investigate(event_for(s), s, ctx)
        self.assertEqual(d.root_cause.id, "bad_deploy:payment")
        self.assertTrue(any("consistency" in x for x in d.root_cause.supporting))
        self.assertGreaterEqual(d.confidence, 0.9)

    def test_silent_status_mismatch_is_detected_and_diagnosed(self):
        s = snap()                       # every service healthy AND checkout works...
        s["consistency"] = cons("failed", "2 new order(s) whose status doesn't match a captured payment")
        m = Monitor()
        m.check_for_incident(s)
        ev = m.check_for_incident(s)     # ...but the consistency check still raises an incident
        self.assertIsNotNone(ev)
        self.assertEqual(ev.affected_services, ["payment"])
        ctx = Context(read_logs=lambda c: [], recent_changes=[change(after={"version": "2.5"})])
        d = investigate(ev, s, ctx)
        self.assertEqual(d.root_cause.id, "bad_deploy:payment")
        self.assertGreaterEqual(d.confidence, 0.8)

    def test_consistency_ok_means_no_incident(self):
        s = snap()
        s["consistency"] = cons("ok")
        m = Monitor()
        self.assertIsNone(m.check_for_incident(s))
        self.assertIsNone(m.check_for_incident(s))


class Advanced(unittest.TestCase):
    def test_redis_down(self):
        s = with_redis(snap(storefront="failed", probe="failed"), "exited")
        d = investigate(event_for(s), s, Context(read_logs=lambda c: []))
        self.assertEqual(d.root_cause.id, "container_down:redis")
        self.assertIn("storefront", d.symptoms)
        p = rehearsed(plan(d))
        self.assertEqual((p.operation, p.target), ("start_container", "shop-redis"))
        self.assertEqual(SafetyEngine(SECRET).decide(p).verdict, "ALLOW")

    def test_api_slowdown(self):
        s = snap(probe="failed")
        s["probe"].update(slow=True, detail="too slow: checkout took 1750 ms (limit 1200 ms)")
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 3 if c == "shop-cart" else 0,
                      recent_changes=[change(service="cart", after={"version": "2.6"})])
        d = investigate(event_for(s), s, ctx)
        self.assertEqual(d.root_cause.id, "bad_deploy:cart")
        self.assertTrue(any("too slow" in x for x in d.root_cause.supporting))


class ConsistencyGrowth(unittest.TestCase):
    """The monitor only alarms on NEW anomalies, so old ones don't keep the incident open after the fix."""

    def setUp(self):
        self.answers = []
        fake = types.ModuleType("httpx")

        class R:
            def __init__(self, data):
                self.data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self.data
        fake.get = lambda url, params=None, timeout=3: R(self.answers.pop(0))
        self.saved = sys.modules.get("httpx")
        sys.modules["httpx"] = fake

    def tearDown(self):
        if self.saved is not None:
            sys.modules["httpx"] = self.saved
        else:
            del sys.modules["httpx"]

    def test_growth(self):
        def c(o, m):
            return {"payments_without_order": o, "status_mismatch": m, "unmatched_amount_paise": 0}
        self.answers = [c(3, 0), c(5, 0), c(5, 0), c(5, 2)]
        mon = Monitor()
        self.assertEqual(mon.consistency()["result"], "ok")       # first look: baseline
        r = mon.consistency()
        self.assertEqual(r["result"], "failed")
        self.assertIn("2 new payment", r["detail"])
        self.assertEqual(mon.consistency()["result"], "ok")       # no new ones: healthy again
        self.assertIn("mismatch", str(mon.consistency()))


if __name__ == "__main__":
    unittest.main()
