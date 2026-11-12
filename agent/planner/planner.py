"""Fix planner: turns a diagnosis into ONE action from a fixed catalogue. Never free-form commands."""
import uuid

from agent import changes
from agent.contracts import ActionProposal
from agent.investigation.investigator import dependents_of

CATALOGUE = {
    "container_down": {"operation": "start_container", "rollback": "stop_container (only with human approval)"},
    "oom_killed": {"operation": "start_container",
                   "rollback": "none needed: a fresh start frees the leaked memory (the bug remains: a human should fix it)"},
    "service_unhealthy": {"operation": "restart_container",
                          "rollback": "none needed: restart keeps the same release and configuration"},
    "bad_deploy": {"operation": "rollback_release", "rollback": "redeploy release {bad} (only if a human asks)"},
    "bad_config": {"operation": "restore_config", "rollback": "re-apply the reverted config {bad} (only if a human asks)"},
}


def plan(diagnosis, known_good=None):
    cause = diagnosis.root_cause
    if cause is None or cause.kind not in CATALOGUE:
        return None  # nothing safe to propose: escalate to a human
    entry = CATALOGUE[cause.kind]
    params = {"service": cause.service}
    rollback = entry["rollback"]

    if cause.kind in ("bad_deploy", "bad_config"):
        kind = "release" if cause.kind == "bad_deploy" else "config"
        known_good = known_good or changes.KnownGood()
        good, current = known_good.get(cause.service, kind), changes.read_runtime(cause.service, kind)
        if good == current:
            return None  # the running version IS the known-good one: rolling back would change nothing
        params.update({"kind": kind, "from": current, "to": good, "blamed_change": cause.change_id})
        rollback = rollback.format(bad=current.get("version") if kind == "release" else current)

    verify = [cause.service] + dependents_of(cause.service)
    for svc in list(verify):
        verify += [d for d in dependents_of(svc) if d not in verify]
    return ActionProposal(
        action_id="ACT-" + uuid.uuid4().hex[:6].upper(),
        incident_id=diagnosis.incident_id,
        operation=entry["operation"],
        target=cause.container,
        parameters=params,
        expected_outcome=[f"{s} healthy" for s in verify] + ["customer checkout succeeds"],
        verify_services=verify,
        reversible=True,
        rollback=rollback,
        simulation_required=False,  # Phase 2b: shadow simulation
        diagnosis_confidence=diagnosis.confidence,
    )
