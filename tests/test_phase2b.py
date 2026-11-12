"""Phase 2b: shadow simulation (reproduce first, then fix). Fake shadow shop, no Docker.
Run from the repo root:  python -m unittest tests.test_phase2b -v
"""
import unittest

from agent import changes
from agent.contracts import ActionProposal
from agent.safety.engine import SafetyEngine
from agent.shadow.simulator import SHADOW_CONTAINERS, ShadowRuntime, ShadowSimulator
from tests.test_phase2 import TempData

SECRET = "test-secret"


class FakeShadowShop:
    """Checkout works only if every shadow container runs, payment is not 2.3 and points at port 5432."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.status = {n: "running" for n in SHADOW_CONTAINERS}
        self.calls = []
        self.broken_forever = False

    def probe(self):
        if self.broken_forever:
            return False, "HTTP 503"
        ok = (all(s == "running" for s in self.status.values())
              and self.runtime.read("payment", "release")["version"] != "2.3"
              and self.runtime.read("payment", "config")["db_port"] == 5432)
        return ok, "HTTP 200" if ok else "HTTP 503"

    def container(self, name):
        shop = self

        class C:
            status = shop.status[name]

            def start(self):
                shop.calls.append(("start", name)); shop.status[name] = "running"

            def stop(self, timeout=3):
                shop.calls.append(("stop", name)); shop.status[name] = "exited"

            def restart(self, timeout=3):
                shop.calls.append(("restart", name)); shop.status[name] = "running"
        return C()


def proposal(op, target, **params):
    return ActionProposal(action_id="ACT-T", incident_id="SR-T", operation=op, target=target,
                          parameters=params, expected_outcome=["x"], verify_services=["payment"],
                          reversible=True, diagnosis_confidence=0.95)


class Shadow(TempData):
    def setUp(self):
        super().setUp()
        self.rt = ShadowRuntime()
        self.shop = FakeShadowShop(self.rt)
        self.sim = ShadowSimulator(probe=self.shop.probe, runtime=self.rt, sleep=lambda s: None,
                                   reproduce_timeout_s=0.2, verify_timeout_s=0.2, interval_s=0)
        def guarded(name):  # same hard wall as the real simulator, then the fake shop
            if name not in SHADOW_CONTAINERS:
                raise PermissionError(name)
            return self.shop.container(name)
        self.sim._container = guarded

    def stages(self, r):
        return [(s["stage"], s["status"]) for s in r["steps"]]

    def test_db_down_rehearsal_passes(self):
        r = self.sim.simulate(proposal("start_container", "demo-db"))
        self.assertEqual(r["status"], "passed")
        self.assertTrue(r["reproduced"] and r["fixed"])
        self.assertEqual(self.stages(r)[:4], [("baseline", "ok"), ("reproduce", "red"), ("apply", "done"),
                                              ("verify", "green")])
        self.assertIn(("stop", "shadow-db"), self.shop.calls)
        self.assertNotIn("demo-db", [n for _, n in self.shop.calls])  # never the live database

    def test_bad_deploy_rollback_rehearsal_passes_and_copy_is_restored(self):
        r = self.sim.simulate(proposal("rollback_release", "shop-payment", service="payment", kind="release",
                                       **{"from": {"version": "2.3"}, "to": {"version": "2.2"}}))
        self.assertEqual(r["status"], "passed")
        self.assertEqual(self.rt.read("payment", "release"), {"version": "2.2"})

    def test_broken_config_rehearsal(self):
        bad = {**changes.DEFAULTS["config"], "db_port": 5433}
        r = self.sim.simulate(proposal("restore_config", "shop-payment", service="payment", kind="config",
                                       **{"from": bad, "to": dict(changes.DEFAULTS["config"])}))
        self.assertEqual(r["status"], "passed")

    def test_fix_that_does_not_work_fails_and_is_blocked(self):
        r = self.sim.simulate(proposal("rollback_release", "shop-payment", service="payment", kind="release",
                                       **{"from": {"version": "2.3"}, "to": {"version": "2.3"}}))
        self.assertEqual(r["status"], "failed")
        self.assertEqual(self.rt.read("payment", "release"), {"version": "2.2"})  # copy restored anyway
        p = proposal("rollback_release", "shop-payment", simulation=r)
        self.assertEqual(SafetyEngine(SECRET).decide(p).verdict, "BLOCK")

    def test_cannot_reproduce_means_human_must_approve(self):
        r = self.sim.simulate(proposal("rollback_release", "shop-payment", service="payment", kind="release",
                                       **{"from": {"version": "2.4"}, "to": {"version": "2.2"}}))
        self.assertEqual(r["status"], "not_reproducible")
        d = SafetyEngine(SECRET).decide(proposal("rollback_release", "shop-payment", simulation=r))
        self.assertEqual(d.verdict, "APPROVAL_REQUIRED")
        self.assertTrue(any("could not be reproduced" in x for x in d.reasons))

    def test_generic_restart_is_not_reproducible(self):
        self.assertEqual(self.sim.simulate(proposal("restart_container", "shop-cart"))["status"], "not_reproducible")

    def test_unhealthy_shadow_means_error_not_pass(self):
        self.shop.broken_forever = True
        r = ShadowSimulator(probe=self.shop.probe, runtime=self.rt, sleep=lambda s: None,
                            reproduce_timeout_s=0.1, verify_timeout_s=0.1, interval_s=0)
        r._container = self.sim._container
        r._wait_for = lambda want_ok, t, needed=1: (False, "HTTP 503", 0)
        out = r.simulate(proposal("start_container", "demo-db"))
        self.assertEqual(out["status"], "error")
        self.assertEqual(SafetyEngine(SECRET).decide(proposal("start_container", "demo-db", simulation=out)).verdict,
                         "APPROVAL_REQUIRED")

    def test_hard_wall_around_live_containers(self):
        sim = ShadowSimulator()
        sim._client = lambda: (_ for _ in ()).throw(AssertionError("must not even reach docker"))
        for live in ("demo-db", "shop-payment", "docker-proxy-exec"):
            with self.assertRaises(PermissionError):
                sim._container(live)

    def test_no_rehearsal_at_all_needs_a_human(self):
        self.assertEqual(SafetyEngine(SECRET).decide(proposal("start_container", "demo-db")).verdict,
                         "APPROVAL_REQUIRED")


if __name__ == "__main__":
    unittest.main()
