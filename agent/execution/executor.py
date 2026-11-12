"""Execution engine: the ONLY component that changes anything.

It accepts a structured action plus a signed grant, re-checks both, and runs one of a
handful of named operations through the write proxy. There is no shell and no free-form command.
"""
import time

from agent import config
from agent.safety.engine import verify_grant

IMPLEMENTED = {"start_container", "restart_container"}


class Executor:
    def __init__(self, secret, policy):
        self.secret = secret
        self.policy = policy
        self._docker = None

    def _client(self):
        if self._docker is None:
            import docker
            self._docker = docker.DockerClient(base_url=config.DOCKER_EXEC_URL, timeout=20)
        return self._docker

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
            container = self._client().containers.get(proposal.target)
            if proposal.operation == "start_container":
                container.start()
            elif proposal.operation == "restart_container":
                container.restart(timeout=10)
        except Exception as e:
            self._docker = None
            return {"ok": False, "detail": f"docker error: {str(e)[:200]}"}
        return {"ok": True, "detail": f"{proposal.operation} {proposal.target} done",
                "duration_s": round(time.time() - started, 2)}
