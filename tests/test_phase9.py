"""On-call engineer approval for risky fixes (frozen database / Redis).
Run from the repo root:  python -m unittest tests.test_phase9 -v
"""
import unittest

from agent.investigation.investigator import Context, investigate
from agent.planner.planner import plan
from agent.safety.engine import SafetyEngine
from agent.execution.executor import Executor
from agent.shadow.simulator import ShadowRuntime, ShadowSimulator
from tests.test_phase1 import event_for, rehearsed, snap
from tests.test_phase2 import TempData
from tests.test_phase2b import FakeShadowShop, proposal
from tests.test_phase5 import with_redis
from tests.test_phase6 import granted

SECRET = "test-secret"


def frozen_db():
    s = snap(cart="failed", payment="failed", probe="failed")
    s["services"]["database"].update(container_status="paused", health="failed", detail="container is paused")
    return s


class Frozen(unittest.TestCase):
    def test_frozen_db_diagnosed_and_restart_planned(self):
        s = frozen_db()
        d = investigate(event_for(s), s, Context(read_logs=lambda c: []))
        self.assertEqual(d.root_cause.id, "service_unhealthy:database")
        self.assertIn("frozen", d.root_cause.supporting[0])
        p = plan(d)
        self.assertEqual((p.operation, p.target), ("restart_container", "demo-db"))
        self.assertTrue(p.parameters["frozen"])

    def test_policy_needs_engineer_with_plain_reason(self):
        for target, words in (("demo-db", "every customer and every order"), ("shop-redis", "logs every customer out")):
            d = SafetyEngine(SECRET).decide(rehearsed(proposal("restart_container", target)))
            self.assertEqual(d.verdict, "APPROVAL_REQUIRED")
            self.assertTrue(any(r.startswith("engineer approval required") and words in r for r in d.reasons), d.reasons)
            self.assertIsNone(d.grant)

    def test_normal_fixes_stay_automatic(self):
        self.assertEqual(SafetyEngine(SECRET).decide(rehearsed(proposal("start_container", "demo-db"))).verdict, "ALLOW")


class FrozenExecution(TempData):
    def test_executor_unfreezes_then_restarts(self):
        calls = []

        class C:
            status = "paused"
            def unpause(self): calls.append("unpause")
            def restart(self, timeout=10): calls.append("restart")
        ex = Executor(SECRET, SafetyEngine(SECRET).policy)
        ex._container = lambda n: C()
        p = proposal("restart_container", "demo-db")
        self.assertTrue(ex.execute(p, granted(p))["ok"])
        self.assertEqual(calls, ["unpause", "restart"])

    def test_practice_run_freezes_the_copy(self):
        rt = ShadowRuntime()
        shop = FakeShadowShop(rt)
        real = shop.container

        def container(name):
            c = real(name)
            c.pause = lambda: (shop.calls.append(("pause", name)), shop.status.__setitem__(name, "paused"))
            c.unpause = lambda: (shop.calls.append(("unpause", name)), shop.status.__setitem__(name, "running"))
            return c
        sim = ShadowSimulator(probe=shop.probe, runtime=rt, sleep=lambda s: None,
                              reproduce_timeout_s=0.2, verify_timeout_s=0.2, interval_s=0)
        sim._container = container
        r = sim.simulate(proposal("restart_container", "demo-db", service="database", frozen=True))
        self.assertEqual(r["status"], "passed", r["steps"])
        self.assertIn(("pause", "shadow-db"), shop.calls)
        self.assertNotIn("demo-db", [n for _, n in shop.calls])


if __name__ == "__main__":
    unittest.main()
