"""Every action is reversible: snapshot + undo, refuse-if-changed, hold instead of asking, auto-undo.
Run from the repo root:  python -m unittest tests.test_phase6 -v
"""
import time
import unittest

from agent import changes
from agent.contracts import ActionProposal, Grant, PolicyDecision
from agent.execution.executor import Executor
from agent.safety.engine import SafetyEngine, sign
from tests.test_phase1 import rehearsed
from tests.test_phase2 import TempData

SECRET = "test-secret"


class FakeDocker:
    def __init__(self):
        self.status = {}
        self.calls = []

    def container(self, name):
        d = self

        class C:
            status = d.status.get(name, "exited")

            def start(self):
                d.calls.append(("start", name)); d.status[name] = "running"

            def stop(self, timeout=10):
                d.calls.append(("stop", name)); d.status[name] = "exited"

            def restart(self, timeout=10):
                d.calls.append(("restart", name)); d.status[name] = "running"
        return C()


def proposal(op, target, **params):
    return ActionProposal(action_id="ACT-1", incident_id="SR-1", operation=op, target=target, parameters=params,
                          expected_outcome=["x"], verify_services=[], reversible=True, diagnosis_confidence=0.95)


def granted(p):
    exp = time.time() + 60
    return PolicyDecision(decision_id="D", action_id=p.action_id, verdict="ALLOW", reasons=[],
                          grant=Grant(action_id=p.action_id, operation=p.operation, target=p.target, expires_at=exp,
                                      signature=sign(SECRET, p.action_id, p.operation, p.target, exp)))


class Reversible(TempData):
    def setUp(self):
        super().setUp()
        self.docker = FakeDocker()
        self.ex = Executor(SECRET, SafetyEngine(SECRET).policy)
        self.ex._container = self.docker.container

    def run_undo(self, undo):
        p = proposal("undo", undo["target"], undo=undo)
        return self.ex.execute(p, granted(p))

    def test_rollback_has_exact_undo(self):
        changes.write_runtime("payment", "release", {"version": "2.3"})
        p = proposal("rollback_release", "shop-payment", service="payment", kind="release",
                     **{"from": {"version": "2.3"}, "to": {"version": "2.2"}})
        r = self.ex.execute(p, granted(p))
        self.assertTrue(r["ok"] and r["reversible"])
        self.assertEqual(r["undo"]["restore"], {"version": "2.3"})
        u = self.run_undo(r["undo"])
        self.assertTrue(u["ok"], u)
        self.assertEqual(changes.read_runtime("payment", "release"), {"version": "2.3"})
        self.assertTrue(changes.all_changes()[-1]["is_rollback"])

    def test_undo_refuses_if_someone_changed_it_since(self):
        changes.write_runtime("payment", "config", {**changes.DEFAULTS["config"], "db_port": 5433})
        p = proposal("restore_config", "shop-payment", service="payment", kind="config",
                     **{"from": {**changes.DEFAULTS["config"], "db_port": 5433}, "to": dict(changes.DEFAULTS["config"])})
        r = self.ex.execute(p, granted(p))
        changes.write_runtime("payment", "config", {**changes.DEFAULTS["config"], "db_port": 6000})  # a human changed it
        u = self.run_undo(r["undo"])
        self.assertFalse(u["ok"])
        self.assertEqual(changes.read_runtime("payment", "config")["db_port"], 6000)

    def test_start_container_undo_restores_previous_state(self):
        p = proposal("start_container", "demo-db")
        r = self.ex.execute(p, granted(p))
        self.assertEqual(r["undo"]["restore_status"], "exited")
        self.assertTrue(self.run_undo(r["undo"])["ok"])
        self.assertEqual(self.docker.status["demo-db"], "exited")

    def test_restart_needs_no_undo(self):
        self.docker.status["shop-cart"] = "running"
        p = proposal("restart_container", "shop-cart")
        r = self.ex.execute(p, granted(p))
        self.assertIsNone(r["undo"])
        self.assertIn("nothing to undo", r["snapshot_note"])

    def test_undo_needs_a_valid_signature(self):
        p = proposal("undo", "demo-db", undo={"kind": "container", "target": "demo-db", "restore_status": "exited",
                                               "describe": "x"})
        d = granted(p)
        d.grant.signature = "forged"
        self.assertIn("bad signature", self.ex.execute(p, d)["detail"])

    def test_undo_only_on_shop_containers(self):
        p = proposal("undo", "docker-proxy-exec", undo={"kind": "container", "target": "docker-proxy-exec",
                                                         "restore_status": "exited", "describe": "x"})
        self.assertIn("not allowed", self.ex.execute(p, granted(p))["detail"])


class HoldNotAsk(unittest.TestCase):
    def test_policy_settings(self):
        rules = SafetyEngine(SECRET).policy["rules"]
        self.assertFalse(rules["human_approval"])
        self.assertTrue(rules["auto_undo_on_failed_verification"])


if __name__ == "__main__":
    unittest.main()
