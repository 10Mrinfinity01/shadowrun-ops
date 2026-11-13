"""Postmortem: a short incident report written after every incident. Built from facts, no AI (0 tokens)."""

PREVENT = {
    "bad_deploy": "Add a real checkout test to the release pipeline for {service}, and block releases that fail it.",
    "bad_config": "Validate {service}'s config before it is applied (types, ranges, reachable hosts).",
    "container_down": "Set a restart policy for {service} and alert on unexpected exits.",
    "oom_killed": "{service} ran out of memory: find the leak and set a memory limit with headroom.",
    "service_unhealthy": "Find out why {service} stopped answering; add a liveness check so it is restarted early.",
}


def build_postmortem(inc, tokens):
    d = inc.get("diagnosis") or {}
    rc = d.get("root_cause") or {}
    p = inc.get("proposal") or {}
    agent = inc.get("agent") or {}
    approval = next((s for s in reversed(inc.get("steps", [])) if s.get("stage") == "approval"), None)
    memory = agent.get("memory") or {}
    tool_calls = sum(len(i.get("steps", [])) for i in agent.get("investigations", []))
    if memory.get("hit"):
        cost = "0 AI tokens: the problem was recognised from memory"
    elif tokens.get("used"):
        cost = f"{tokens['used']} AI tokens in {tokens['llm_calls']} call(s), {tool_calls} tool call(s)"
    else:
        cost = "0 AI tokens: the rule engine decided"
    fix = f"{p.get('operation')} on {p.get('target')}" if p else "none"
    if p.get("parameters", {}).get("to", {}).get("version"):
        fix += f" (back to version {p['parameters']['to']['version']})"
    return {
        "title": f"{inc['id']}: {rc.get('id', 'unknown cause')}",
        "status": inc.get("status"),
        "what_broke": ", ".join((inc.get("event") or {}).get("affected_services", [])),
        "root_cause": d.get("explanation", "not found"),
        "evidence": (rc.get("supporting") or [])[:4],
        "fix": fix,
        "practice_run": (inc.get("simulation") or {}).get("status", "not run"),
        "approval": approval["detail"] if approval else "not needed (reversible, inside the rules)",
        "time_to_recover_s": inc.get("recovery_seconds"),
        "agent_path": " → ".join(t["node"] for t in agent.get("trail", [])),
        "ai_cost": cost,
        "prevent_it": PREVENT.get(rc.get("kind"), "An engineer should find the permanent fix.").format(
            service=rc.get("service", "the service")),
    }
