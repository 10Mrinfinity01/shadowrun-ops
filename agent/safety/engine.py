"""Independent Safety & Policy engine. Plain code and a YAML file: not another AI opinion.

Decides ALLOW / APPROVAL_REQUIRED / BLOCK and signs a short-lived grant that the
Execution engine checks. The AI never sees the signing secret.
"""
import hashlib
import hmac
import json
import pathlib
import time
import uuid

import yaml

from agent.contracts import Grant, PolicyDecision

POLICY_PATH = pathlib.Path(__file__).with_name("policy.yaml")


def load_policy(path=POLICY_PATH):
    return yaml.safe_load(pathlib.Path(path).read_text())


def _payload(action_id, operation, target, expires_at):
    return json.dumps({"a": action_id, "o": operation, "t": target, "e": expires_at}, sort_keys=True).encode()


def sign(secret, action_id, operation, target, expires_at):
    return hmac.new(secret.encode(), _payload(action_id, operation, target, expires_at), hashlib.sha256).hexdigest()


def verify_grant(secret, grant, proposal, now=None):
    now = now or time.time()
    if not secret:
        return False, "no GRANT_SECRET configured"
    if grant.action_id != proposal.action_id or grant.operation != proposal.operation or grant.target != proposal.target:
        return False, "grant does not match this action"
    if now > grant.expires_at:
        return False, "grant expired"
    expected = sign(secret, grant.action_id, grant.operation, grant.target, grant.expires_at)
    if not hmac.compare_digest(expected, grant.signature):
        return False, "bad signature"
    return True, "ok"


class SafetyEngine:
    def __init__(self, secret, policy=None):
        self.secret = secret
        self.policy = policy or load_policy()
        self.last_action_at = {}  # target -> timestamp
        self.attempts = {}        # incident_id -> count

    def record_execution(self, proposal, now=None):
        self.last_action_at[proposal.target] = now or time.time()
        self.attempts[proposal.incident_id] = self.attempts.get(proposal.incident_id, 0) + 1

    def _make(self, proposal, verdict, reasons, now):
        grant = None
        if verdict in ("ALLOW", "APPROVAL_REQUIRED") and self.secret:
            expires = now + self.policy["rules"]["grant_ttl_seconds"]
            grant = Grant(action_id=proposal.action_id, operation=proposal.operation, target=proposal.target,
                          expires_at=expires,
                          signature=sign(self.secret, proposal.action_id, proposal.operation, proposal.target, expires))
        if verdict == "APPROVAL_REQUIRED":
            grant = None  # issued only after a human approves (see approve())
        return PolicyDecision(decision_id="DEC-" + uuid.uuid4().hex[:6].upper(), action_id=proposal.action_id,
                              verdict=verdict, reasons=reasons, grant=grant)

    def decide(self, proposal, kill_switch=False, always_ask=False, now=None):
        now = now or time.time()
        p, rules = self.policy, self.policy["rules"]
        if kill_switch:
            return self._make(proposal, "BLOCK", ["kill switch is ON: the agent may not act"], now)
        if not self.secret:
            return self._make(proposal, "BLOCK", ["GRANT_SECRET missing in .env: cannot sign permissions"], now)
        if proposal.operation in p["denied_actions"]:
            return self._make(proposal, "BLOCK", [f"'{proposal.operation}' is on the denied list"], now)
        if proposal.target in p["protected_targets"]:
            return self._make(proposal, "BLOCK", [f"'{proposal.target}' is protected"], now)
        allowed = p["allowed_actions"].get(proposal.operation)
        if not allowed:
            return self._make(proposal, "BLOCK", [f"'{proposal.operation}' is not in the allowed catalogue"], now)
        if proposal.target not in allowed["targets"]:
            return self._make(proposal, "BLOCK", [f"'{proposal.target}' is not an allowed target for {proposal.operation}"], now)
        if self.attempts.get(proposal.incident_id, 0) >= rules["max_attempts_per_incident"]:
            return self._make(proposal, "BLOCK", ["maximum fix attempts for this incident reached: wake a human"], now)
        last = self.last_action_at.get(proposal.target)
        if last and rules.get("cooldown_seconds_per_target", 0) and now - last < rules["cooldown_seconds_per_target"]:
            wait = int(rules["cooldown_seconds_per_target"] - (now - last))
            return self._make(proposal, "BLOCK", [f"cooldown: {proposal.target} was changed recently, wait {wait}s"], now)
        sim = proposal.parameters.get("simulation") or {}
        sim_status = sim.get("status")
        if rules.get("require_simulation") and sim_status == "failed":
            return self._make(proposal, "BLOCK", ["the fix did NOT work in the shadow copy: refusing to try it on "
                                                  "the real shop"], now)

        reasons = [f"'{proposal.operation}' on '{proposal.target}' is allowed by policy"]
        needs_human = []
        if rules.get("require_simulation"):
            if sim_status == "passed":
                reasons.append("rehearsed in the shadow copy: fault reproduced, fix worked")
            elif sim_status == "not_reproducible":
                needs_human.append("the fault could not be reproduced in the shadow copy, so the rehearsal proves nothing")
            else:
                needs_human.append("no successful shadow rehearsal (" + (sim_status or "not run") + ")")
        if proposal.target in allowed.get("approval_targets", []):
            why = p.get("approval_reasons", {}).get(proposal.target, f"{proposal.operation} on {proposal.target} is risky")
            needs_human.append(f"engineer approval required: {why}")
        if proposal.diagnosis_confidence < rules["min_confidence_for_auto"]:
            needs_human.append(f"diagnosis confidence {proposal.diagnosis_confidence:.2f} is below "
                               f"{rules['min_confidence_for_auto']}")
        if not proposal.reversible:
            needs_human.append("action is not reversible")
        if always_ask:
            needs_human.append("approval mode is 'always ask': an engineer authorises every fix")
        if needs_human:
            return self._make(proposal, "APPROVAL_REQUIRED", reasons + needs_human, now)
        return self._make(proposal, "ALLOW", reasons + ["confidence is high and the action is reversible"], now)

    def approve(self, proposal, decision, approver, now=None):
        """Called only when a human clicks Approve. Issues the signed grant."""
        now = now or time.time()
        expires = now + self.policy["rules"]["grant_ttl_seconds"]
        decision.grant = Grant(action_id=proposal.action_id, operation=proposal.operation, target=proposal.target,
                               expires_at=expires,
                               signature=sign(self.secret, proposal.action_id, proposal.operation, proposal.target, expires))
        decision.reasons.append(f"approved by {approver}")
        return decision
