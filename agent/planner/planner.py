"""Fix planner: turns a diagnosis into ONE action from a fixed catalogue. Never free-form commands."""
import uuid

from agent import config
from agent.contracts import ActionProposal
from agent.investigation.investigator import dependents_of

CATALOGUE = {
    "container_down": {
        "operation": "start_container",
        "reversible": True,
        "rollback": "stop_container (only with human approval)",
    },
    "service_unhealthy": {
        "operation": "restart_container",
        "reversible": True,
        "rollback": "none needed: restart returns the same version and configuration",
    },
}


def plan(diagnosis):
    cause = diagnosis.root_cause
    if cause is None or cause.kind not in CATALOGUE:
        return None  # nothing safe to propose: escalate to a human
    entry = CATALOGUE[cause.kind]
    verify = [cause.service] + dependents_of(cause.service)
    for svc in list(verify):
        verify += [d for d in dependents_of(svc) if d not in verify]
    return ActionProposal(
        action_id="ACT-" + uuid.uuid4().hex[:6].upper(),
        incident_id=diagnosis.incident_id,
        operation=entry["operation"],
        target=cause.container,
        parameters={"service": cause.service},
        expected_outcome=[f"{s} healthy" for s in verify] + ["customer checkout succeeds"],
        verify_services=verify,
        reversible=entry["reversible"],
        rollback=entry["rollback"],
        simulation_required=False,  # Phase 2: shadow simulation
        diagnosis_confidence=diagnosis.confidence,
    )
