"""The four contracts between modules. Change them only together with the other laptop.

Run `python -m agent.contracts` to regenerate the JSON Schema files in contracts/.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class Signal(BaseModel):
    type: Literal["health_check", "container_state", "customer_probe"]
    target: str
    result: Literal["ok", "failed"]
    detail: str = ""


# 1. Monitoring -> Investigation
class IncidentEvent(BaseModel):
    incident_id: str
    detected_at: str
    severity: Literal["low", "high", "critical"]
    affected_services: list[str]
    signals: list[Signal]
    evidence_refs: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    id: str  # e.g. "container_down:database"
    kind: Literal["container_down", "service_unhealthy"]
    service: str
    container: str
    score: float
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)


# 2. Investigation -> Planner
class Diagnosis(BaseModel):
    incident_id: str
    root_cause: Optional[Hypothesis]
    alternatives: list[Hypothesis] = Field(default_factory=list)
    confidence: float
    explanation: str
    symptoms: list[str] = Field(default_factory=list)
    suspicious_log_lines: list[str] = Field(default_factory=list)
    llm_provider: Optional[str] = None
    llm_agreed: Optional[bool] = None


# 3. Planner -> Shadow -> Safety -> Execution
class ActionProposal(BaseModel):
    action_id: str
    incident_id: str
    operation: str
    target: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    expected_outcome: list[str]
    verify_services: list[str]
    reversible: bool
    rollback: Optional[str] = None
    simulation_required: bool = False
    diagnosis_confidence: float


class Grant(BaseModel):
    action_id: str
    operation: str
    target: str
    expires_at: float
    signature: str


# 4. Safety -> Execution
class PolicyDecision(BaseModel):
    decision_id: str
    action_id: str
    verdict: Literal["ALLOW", "APPROVAL_REQUIRED", "BLOCK"]
    reasons: list[str]
    grant: Optional[Grant] = None


if __name__ == "__main__":
    import json
    import pathlib

    out = pathlib.Path(__file__).resolve().parent.parent / "contracts"
    out.mkdir(exist_ok=True)
    for name, model in [("incident_event", IncidentEvent), ("diagnosis", Diagnosis),
                        ("action_proposal", ActionProposal), ("policy_decision", PolicyDecision)]:
        (out / f"{name}.schema.json").write_text(json.dumps(model.model_json_schema(), indent=2))
        print("wrote", name)
