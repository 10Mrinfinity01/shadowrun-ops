"""Execution engine: the ONLY component that changes anything.

It accepts a structured action plus a signed grant, re-checks both, and runs one of a
handful of named operations. There is no shell and no free-form command.

Every action is reversible: before changing anything it records a snapshot of what was there,
and returns an `undo` recipe. `undo` puts back exactly that snapshot, and only if nobody has
changed things since.
"""
import time

from agent import changes, config
from agent.safety.engine import verify_grant

IMPLEMENTED = {"start_container", "restart_container", "rollback_release", "restore_config", "undo"}


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

    def _check(self, proposal, decision):
        if decision.verdict == "BLOCK" or decision.grant is None:
            return "refused: no valid permission"
        ok, why = verify_grant(self.secret, decision.grant, proposal)
        if not ok:
            return f"refused: {why}"
        if proposal.operation not in IMPLEMENTED:
            return f"refused: {proposal.operation} is not implemented"
        allowed = self.policy["allowed_actions"].get(proposal.operation, {}).get("targets", [])
        if proposal.target not in allowed:  # belt and braces: re-check the allowlist here too
            return f"refused: {proposal.target} not allowed"
        return None

    def execute(self, proposal, decision):
        started = time.time()
        refused = self._check(proposal, decision)
        if refused:
            return {"ok": False, "detail": refused}
        try:
            op, p = proposal.operation, proposal.parameters
            if op == "undo":
                return self._undo(proposal, started)
            if op == "start_container":
                c = self._container(proposal.target)
                was = c.status
                c.start()
                detail = f"started {proposal.target}"
                undo = {"kind": "container", "target": proposal.target, "restore_status": was,
                        "describe": f"stop {proposal.target} again (it was '{was}' before the fix)"}
            elif op == "restart_container":
                c = self._container(proposal.target)
                if c.status == "paused":  # a frozen container must be unfrozen before it can restart cleanly
                    c.unpause()
                c.restart(timeout=10)
                detail = f"restarted {proposal.target}"
                undo = None  # same release, same config, same data: there is nothing to put back
            else:  # rollback_release / restore_config: put back the known-good file, then restart
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
                undo = {"kind": "runtime", "target": proposal.target, "service": service, "runtime_kind": kind,
                        "restore": current, "expect": p["to"],
                        "describe": f"put {service} {kind} back to "
                                    + (f"release {current.get('version')}" if kind == "release" else str(current))}
        except Exception as e:
            self._docker = None
            return {"ok": False, "detail": f"error: {str(e)[:200]}"}
        return {"ok": True, "detail": detail, "duration_s": round(time.time() - started, 2),
                "undo": undo, "reversible": True,
                "snapshot_note": "nothing to undo: a restart changes no code, config or data" if undo is None else None}

    def _undo(self, proposal, started):
        u = proposal.parameters["undo"]
        if u["kind"] == "container":
            c = self._container(u["target"])
            if c.status != "running":
                return {"ok": False, "detail": "refused: the container is no longer in the state the agent left it in"}
            if u["restore_status"] != "running":
                c.stop(timeout=10)
            detail = f"undo: {u['target']} put back to '{u['restore_status']}'"
        else:
            current = self.runtime.read_runtime(u["service"], u["runtime_kind"])
            if current != u["expect"]:
                return {"ok": False, "detail": "refused: someone changed it after the agent's fix; not overwriting"}
            self.runtime.record_change(u["service"], u["runtime_kind"], current, u["restore"], actor="shadowrun-ops",
                                       note=f"undo of the agent's fix for {proposal.incident_id}", is_rollback=True)
            self.runtime.write_runtime(u["service"], u["runtime_kind"], u["restore"])
            self._container(u["target"]).restart(timeout=10)
            detail = f"undo: {u['describe']}"
        return {"ok": True, "detail": detail, "duration_s": round(time.time() - started, 2), "undo": None}
