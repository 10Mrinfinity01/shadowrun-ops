"""Phase 2a logic tests: bad deploy, broken config, OOM, change ledger, known-good store, rollback.
No Docker, no internet. Run from the repo root:  python -m unittest tests.test_phase2 -v
"""
import json
import pathlib
import tempfile
import time
import unittest

from agent import changes
from agent.execution.executor import Executor
from agent.investigation.investigator import Context, investigate, rank_hypotheses
from agent.monitoring.monitor import Monitor
from agent.planner.planner import plan
from agent.safety.engine import SafetyEngine
from tests.test_phase1 import DB_DOWN, event_for, rehearsed, snap

SECRET = "test-secret"


def change(service="payment", kind="release", before=None, after=None, age_s=30, cid="CHG-1"):
    before = before or ({"version": "2.2"} if kind == "release" else dict(changes.DEFAULTS["config"]))
    after = after or ({"version": "2.3"} if kind == "release" else {**changes.DEFAULTS["config"], "db_port": 5433})
    return {"id": cid, "time": time.time() - age_s, "service": service, "kind": kind, "before": before,
            "after": after, "actor": "dev-ravi", "note": "", "is_rollback": False, "is_reset": False,
            "deploy_window_until": 0}


BAD_DEPLOY = snap(probe="failed")                    # every service healthy, but checkout fails
BROKEN_CONFIG = snap(payment="failed", probe="failed")


def oom_snap():
    s = snap(cart="failed", probe="failed", cart_container="exited")
    s["services"]["cart"].update(oom_killed=True, exit_code=137)
    return s


class TempData(unittest.TestCase):
    """Points the ledger, runtime and known-good files at a temp folder."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.saved = (changes.DATA, changes.RUNTIME, changes.LEDGER, changes.KNOWN_GOOD)
        changes.DATA, changes.RUNTIME = self.tmp, self.tmp / "runtime"
        changes.LEDGER, changes.KNOWN_GOOD = self.tmp / "ledger.jsonl", self.tmp / "known_good.json"

    def tearDown(self):
        changes.DATA, changes.RUNTIME, changes.LEDGER, changes.KNOWN_GOOD = self.saved


class Diagnosis(unittest.TestCase):
    def test_bad_deploy_found_from_ledger_and_logs(self):
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 5 if c == "shop-payment" else 0,
                      recent_changes=[change()])
        d = investigate(event_for(BAD_DEPLOY), BAD_DEPLOY, ctx)
        self.assertEqual(d.root_cause.id, "bad_deploy:payment")
        self.assertEqual(d.root_cause.change_id, "CHG-1")
        self.assertGreaterEqual(d.confidence, 0.9)

    def test_change_without_errors_is_weak(self):
        ctx = Context(read_logs=lambda c: [], recent_changes=[change()])
        self.assertLess(rank_hypotheses(BAD_DEPLOY, ctx)[0].score, 0.7)

    def test_broken_config_beats_plain_restart(self):
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 3,
                      recent_changes=[change(kind="config")])
        hyps = rank_hypotheses(BROKEN_CONFIG, ctx)
        self.assertEqual(hyps[0].id, "bad_config:payment")
        self.assertIn("service_unhealthy:payment", [h.id for h in hyps])
        self.assertGreater(hyps[0].score - hyps[1].score, 0.15)

    def test_oom(self):
        s = oom_snap()
        d = investigate(event_for(s), s, Context(read_logs=lambda c: []))
        self.assertEqual(d.root_cause.id, "oom_killed:cart")
        self.assertEqual(plan(d).operation, "start_container")

    def test_db_down_still_wins_over_unrelated_change(self):
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 9, recent_changes=[change()])
        self.assertEqual(rank_hypotheses(DB_DOWN, ctx)[0].id, "container_down:database")

    def test_nothing_wrong_nothing_changed_means_unknown(self):
        d = investigate(event_for(BAD_DEPLOY), BAD_DEPLOY, Context(read_logs=lambda c: []))
        self.assertIsNone(d.root_cause)
        self.assertIsNone(plan(d))


class Ledger(TempData):
    def test_record_and_filter(self):
        changes.record_change("payment", "release", {"version": "2.2"}, {"version": "2.3"}, "dev")
        changes.record_change("payment", "release", {"version": "2.3"}, {"version": "2.2"}, "agent", is_rollback=True)
        changes.record_change("cart", "config", {}, {}, "reset", is_reset=True)
        self.assertEqual([c["actor"] for c in changes.recent_changes()], ["dev"])
        self.assertTrue(changes.in_deploy_window())

    def test_deploy_window_silences_detection(self):
        m = Monitor()
        for _ in range(3):
            self.assertIsNone(m.check_for_incident(DB_DOWN, in_deploy_window=True))

    def test_known_good_promotion(self):
        kg = changes.KnownGood()
        c = changes.record_change("payment", "release", {"version": "2.2"}, {"version": "2.4"}, "dev")
        changes.write_runtime("payment", "release", {"version": "2.4"})
        self.assertEqual(kg.promote_stable(last_unhealthy_at=0, now=c["time"] + 30), [])  # too soon
        self.assertEqual(len(kg.promote_stable(last_unhealthy_at=0, now=c["time"] + 61)), 1)
        self.assertEqual(kg.get("payment", "release"), {"version": "2.4"})

    def test_change_followed_by_trouble_is_never_promoted(self):
        kg = changes.KnownGood()
        c = changes.record_change("payment", "release", {"version": "2.2"}, {"version": "2.3"}, "dev")
        changes.write_runtime("payment", "release", {"version": "2.3"})
        kg.promote_stable(last_unhealthy_at=c["time"] + 5, now=c["time"] + 100)
        self.assertEqual(kg.get("payment", "release"), {"version": "2.2"})


class Rollback(TempData):
    def bad_deploy_proposal(self):
        changes.write_runtime("payment", "release", {"version": "2.3"})
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 4, recent_changes=[change()])
        return rehearsed(plan(investigate(event_for(BAD_DEPLOY), BAD_DEPLOY, ctx), changes.KnownGood()))

    def test_plan_uses_known_good(self):
        p = self.bad_deploy_proposal()
        self.assertEqual(p.operation, "rollback_release")
        self.assertEqual(p.parameters["from"], {"version": "2.3"})
        self.assertEqual(p.parameters["to"], {"version": "2.2"})
        self.assertIn("2.3", p.rollback)

    def test_no_rollback_when_already_known_good(self):
        ctx = Context(read_logs=lambda c: [], errors_since=lambda c, t: 4, recent_changes=[change()])
        self.assertIsNone(plan(investigate(event_for(BAD_DEPLOY), BAD_DEPLOY, ctx), changes.KnownGood()))

    def test_policy_and_execution(self):
        p = self.bad_deploy_proposal()
        eng = SafetyEngine(SECRET)
        d = eng.decide(p)
        self.assertEqual(d.verdict, "ALLOW")
        restarted = []

        class FakeContainer:
            def restart(self, timeout=10):
                restarted.append(True)
        ex = Executor(SECRET, eng.policy)
        ex._container = lambda name: FakeContainer()
        r = ex.execute(p, d)
        self.assertTrue(r["ok"], r)
        self.assertEqual(changes.read_runtime("payment", "release"), {"version": "2.2"})
        self.assertTrue(restarted)
        last = json.loads(changes.LEDGER.read_text().splitlines()[-1])
        self.assertTrue(last["is_rollback"])
        self.assertEqual(last["actor"], "shadowrun-ops")

    def test_execution_refuses_if_something_changed_meanwhile(self):
        p = self.bad_deploy_proposal()
        d = SafetyEngine(SECRET).decide(p)
        changes.write_runtime("payment", "release", {"version": "2.5"})  # someone deployed again
        ex = Executor(SECRET, SafetyEngine(SECRET).policy)
        ex._container = lambda name: (_ for _ in ()).throw(AssertionError("must not touch docker"))
        self.assertIn("changed since the plan", ex.execute(p, d)["detail"])

    def test_rollback_on_database_is_blocked(self):
        p = self.bad_deploy_proposal()
        p.target = "demo-db"
        self.assertEqual(SafetyEngine(SECRET).decide(p).verdict, "BLOCK")


if __name__ == "__main__":
    unittest.main()
