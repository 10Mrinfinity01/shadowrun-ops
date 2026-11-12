"""Execution engine: the ONLY component that changes anything.

It accepts a structured action plus a signed grant, re-checks both, and runs one of a
handful of named operations. There is no shell and no free-form command.
"""
import time

from agent import changes, config
from agent.safety.engine import verify_grant

IMPLEMENTED = {"start_container", "restart_container", "rollback_release", "restore_config"}


class Executor:
    def __init__(self, secret, policy, runtime=changes):
        self.secret = secret
        self.policy = policy
        self.runtime = runtime  # where release/config files live (swappable in tests)
        self._docker = None

    def _client(self):
        if self._docker is None:
            import docker
            self._docker = docker.DockerClient(base_url=config.DOCKER_EXEC_URL, timeout=20)
        return self._docker

    def _container(self, name):
        return self._client().containers.get(name)

    def execute(self, proposal, decision):
        started = time.time()
        if decision.verdict == "BLOCK" or decision.grant is None:
            return {"ok": False, "detail": "refused: no valid permission"}
        ok, why = verify_grant(self.secret, decision.grant, proposal)
        if not ok:
            return {"ok": False, "detail": f"refused: {why}"}
        if proposal.operation not in IMPLEMENTED:
            return {"ok": False, "detail": f"refused: {proposal.operation} is not implemented"}
        allowed = self.policy["allowed_actions"].get(proposal.operation, {}).get("targets", [])
        if proposal.target not in allowed:  # belt and braces: re-check the allowlist here too
            return {"ok": False, "detail": f"refused: {proposal.target} not allowed"}
        try:
            op = proposal.operation
            if op == "start_container":
                self._container(proposal.target).start()
                detail = f"started {proposal.target}"
            elif op == "restart_container":
                self._container(proposal.target).restart(timeout=10)
                detail = f"restarted {proposal.target}"
            else:  # rollback_release / restore_config: put back the known-good file, then restart
                p = proposal.parameters
                service, kind = p["service"], p["kind"]
                current = self.runtime.read_runtime(service, kind)
                if current != p["from"]:
                    return {"ok": False, "detail": "refused: the running version changed since the plan was made"}
                self.runtime.record_change(service, kind, current, p["to"], actor="shadowrun-ops",
                                           note=f"automatic {op} for {proposal.incident_id}", is_rollback=True)
                self.runtime.write_runtime(service, kind, p["to"])
                self._container(proposal.target).restart(timeout=10)
                what = (f"release {current.get('version')} -> {p['to'].get('version')}" if kind == "release"
                        else f"config restored to known-good {p['to']}")
                detail = f"{op} on {proposal.target}: {what}"
        except Exception as e:
            self._docker = None
            return {"ok": False, "detail": f"error: {str(e)[:200]}"}
        return {"ok": True, "detail": detail, "duration_s": round(time.time() - started, 2)}
