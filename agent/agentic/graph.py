r"""The agentic incident workflow, built as a LangGraph state graph.

    triage --(seen before)--> recall ----------------\
       \--(new problem)----> investigate (AI + tools) -> plan -> practise --(failed)--> rethink --> investigate
                                                                 |                         \--> escalate
                                                               safety --(risky)--> ask_engineer (interrupt: waits for a human)
                                                                 |  \--(blocked)--> escalate        |
                                                               execute <---------------(approved)---/
                                                                 |
                                                               verify --(fixed / partly fixed)--> learn (memory + postmortem)
                                                                  \--(not fixed)--> undo --> rethink

Each box is a node: a function that reads the shared case file (the state) and adds to it.
The AI is used in exactly one node (investigate), on a token budget. Everything that changes the shop
(execute, undo) goes through the same policy, signed grants and snapshots as before.
"""
import asyncio
import operator
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Callable, Optional, TypedDict

from agent.agentic.engine import ENGINE, END, START, Command, MemorySaver, StateGraph, interrupt
from agent.agentic.investigator_agent import MAX_TOOL_CALLS, TokenBudget, drop_ruled_out, run_investigation
from agent.agentic.memory import fingerprint
from agent.agentic.postmortem import build_postmortem
from agent.agentic.tools import ToolDeps, Toolbox
from agent.contracts import ActionProposal, Diagnosis, IncidentEvent, PolicyDecision
from agent.investigation.investigator import investigate
from agent.planner.planner import plan as plan_fix
from agent import recurrence
from agent.verification.verifier import judge_partial

NODES = ["triage", "recall", "investigate", "plan", "practise", "rethink", "safety", "ask_engineer",
         "execute", "verify", "undo", "learn", "escalate"]
EDGES = [  # for the dashboard's drawing of the graph
    ("triage", "recall", "seen before"), ("triage", "investigate", "new problem"),
    ("recall", "plan", "memory matches"), ("recall", "investigate", "memory doesn't match"),
    ("investigate", "plan", "cause found"), ("investigate", "escalate", "no cause"),
    ("plan", "practise", ""), ("plan", "escalate", "no safe fix"),
    ("practise", "safety", "passed"), ("practise", "rethink", "failed"),
    ("rethink", "investigate", "try again"), ("rethink", "escalate", "out of ideas"),
    ("safety", "execute", "allowed"), ("safety", "ask_engineer", "risky"), ("safety", "escalate", "blocked"),
    ("ask_engineer", "execute", "approved"), ("ask_engineer", "escalate", "denied"),
    ("execute", "verify", ""), ("execute", "escalate", "failed"),
    ("verify", "learn", "fixed"), ("verify", "undo", "not fixed"), ("undo", "rethink", ""),
]


class IncidentState(TypedDict, total=False):
    """The case file passed from node to node. Plain JSON values only, so it can be checkpointed."""
    incident_id: str
    event: dict
    snap: dict
    fingerprint: str
    fingerprint_text: str
    memory_hit: Optional[dict]
    diagnosis: Optional[dict]
    cause_id: Optional[str]
    ruled_out: list
    loops: int
    proposal: Optional[dict]
    simulation: Optional[dict]
    decision: Optional[dict]
    approval_request: Optional[dict]
    approval: Optional[dict]
    result: Optional[dict]
    outcome: Optional[str]
    verify_seconds: Optional[float]
    escalate_reason: Optional[str]
    trail: Annotated[list, operator.add]


@dataclass
class AgentDeps:
    """Everything the nodes need from the running agent (main.py builds this)."""
    inc: dict
    step: Callable                 # async (inc, stage, status, detail, data=None)
    broadcast: Callable            # async ()
    build_context: Callable        # () -> investigator Context
    monitor: Any
    simulator: Any
    safety: Any
    executor: Any
    known_good: Any
    memory: Any
    obs: Any
    ask_llm: Callable
    history: Callable              # () -> finished incidents
    kill_switch: Callable          # () -> bool
    approval_mode: Callable        # () -> str
    llm_enabled: Callable          # () -> bool
    run_undo: Callable             # (inc, undo, who) -> result
    money_follow_up: Callable      # (inc, proposal) -> dict | None
    security_event: Callable       # (kind, detail, data)
    verify: Callable               # async (services) -> (ok, last_snapshot, seconds)
    wait_for_approval: Callable    # async (inc, request) -> (approved, who)
    on_shadow: Callable = lambda sim: None
    latest_snap: Callable = lambda: {}
    all_changes: Callable = lambda: []
    test_checkout: Callable = lambda: {"result": "unknown"}
    budget: TokenBudget = field(default_factory=lambda: TokenBudget(8000))


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def _rules(d):
    return d.safety.policy.get("rules", {})


def build_graph(d: AgentDeps):
    inc = d.inc
    loop = asyncio.get_running_loop()

    def ping():  # safe from worker threads
        asyncio.run_coroutine_threadsafe(d.broadcast(), loop)

    async def mark(node, note=""):
        entry = {"node": node, "time": now_str(), "note": note}
        inc["agent"]["trail"].append(entry)
        await d.broadcast()
        return [entry]

    def event_of(state):
        return IncidentEvent(**state["event"])

    async def finish_diagnosis(diag):
        inc["diagnosis"] = diag.model_dump(mode="json")
        cause = diag.root_cause.id if diag.root_cause else "unknown"
        await d.step(inc, "diagnosed", "done", f"Root cause: {cause} (confidence {diag.confidence:.2f})", inc["diagnosis"])
        if diag.suspicious_log_lines:
            d.security_event("poisoned_log", f"{len(diag.suspicious_log_lines)} log line(s) tried to give the AI "
                             "instructions. Quarantined: the LLM never saw them.", diag.suspicious_log_lines)
            await d.step(inc, "security", "quarantined",
                         f"{len(diag.suspicious_log_lines)} suspicious log line(s) quarantined and ignored",
                         diag.suspicious_log_lines)

    # ---- nodes ---------------------------------------------------------------------------------
    async def triage(state):
        ctx = await asyncio.to_thread(d.build_context)
        fp, readable = await asyncio.to_thread(fingerprint, state["event"], ctx.recent_changes, ctx.read_logs)
        hit = d.memory.recall(fp)
        inc["agent"]["memory"] = {"fingerprint": fp, "readable": readable, "hit": bool(hit),
                                  "times_fixed": hit["times_fixed"] if hit else 0}
        note = (f"seen before: fixed {hit['times_fixed']}x (last {hit.get('last_incident')}), skipping the AI"
                if hit else "new problem: the AI investigator will look into it")
        return {"fingerprint": fp, "fingerprint_text": readable, "memory_hit": hit,
                "trail": await mark("triage", note)}

    async def recall(state):
        hit = state["memory_hit"]
        await d.step(inc, "investigating", "running",
                     f"Seen this exact problem before ({hit['times_fixed']}x, last {hit.get('last_incident')}). "
                     "Checking the remembered cause against today's evidence: no AI needed (0 tokens).")
        ctx = await asyncio.to_thread(d.build_context)
        rule = await asyncio.to_thread(investigate, event_of(state), state["snap"], ctx, None)
        pool = [h for h in [rule.root_cause] + list(rule.alternatives) if h]
        match = next((h for h in pool if h.kind == hit["cause"]["kind"] and h.service == hit["cause"]["service"]), None)
        if not match:
            return {"memory_hit": None, "trail": await mark("recall", "memory does not match today's evidence: investigating")}
        diag = rule.model_copy(update={
            "root_cause": match, "alternatives": [h for h in pool if h.id != match.id],
            "confidence": round(max(match.score, 0.8), 2), "llm_provider": "memory (0 tokens)", "llm_agreed": None,
            "explanation": f"Same problem as {hit.get('last_incident')} (fixed {hit['times_fixed']} time(s) before): "
                           + "; ".join(match.supporting) + "."})
        inc["agent"]["investigations"].append({"mode": "memory", "steps": [], "final": {"root_cause_id": match.id},
                                               "note": "remembered fix, checked against today's evidence"})
        await finish_diagnosis(diag)
        return {"diagnosis": diag.model_dump(mode="json"), "trail": await mark("recall", f"remembered cause: {match.id}")}

    async def investigate_node(state):
        ruled = state.get("ruled_out", [])
        use_ai = d.llm_enabled()
        await d.step(inc, "investigating", "running",
                     ("AI investigator is gathering evidence with tools: service map, metrics, logs, deploy history"
                      if use_ai else "Reading health checks, container state and sanitized logs (AI off: rules only)"))
        ctx = await asyncio.to_thread(d.build_context)
        event = event_of(state)
        rule = await asyncio.to_thread(investigate, event, state["snap"], ctx, None)
        rule = drop_ruled_out(rule, ruled)
        trace = {"mode": "rules", "steps": [], "final": None, "note": "AI switched off: the rule engine decided"}
        diag = rule
        if rule.root_cause is not None and use_ai:
            toolbox = Toolbox(ToolDeps(obs=d.obs, read_logs=ctx.read_logs, effective_changes=lambda: ctx.recent_changes,
                                       all_changes=d.all_changes, latest_snap=d.latest_snap,
                                       test_checkout=d.test_checkout, root_hint=lambda: None))
            candidates = ([rule.root_cause] + list(rule.alternatives))[:5]
            live = {"mode": "agent", "steps": [], "final": None, "note": "investigating…"}
            inc["agent"]["investigations"].append(live)

            def on_tool(s):
                live["steps"].append(s)
                inc["agent"]["tokens"] = d.budget.public()
                ping()

            diag, trace = await asyncio.to_thread(
                run_investigation, event, rule, candidates, toolbox, d.ask_llm, d.budget, ruled,
                _rules(d).get("max_tool_calls", MAX_TOOL_CALLS), on_tool)
            inc["agent"]["investigations"][-1] = trace
        else:
            if rule.root_cause is None:
                trace["note"] = "no likely cause among the evidence"
            inc["agent"]["investigations"].append(trace)
        inc["agent"]["tokens"] = d.budget.public()
        await finish_diagnosis(diag)
        n = len(trace.get("steps", []))
        note = (f"{n} tool call(s), {d.budget.used} tokens so far · " if trace["mode"] != "rules" else "rules only · ") + \
               (f"cause: {diag.root_cause.id}" if diag.root_cause else "no cause found")
        out = {"diagnosis": diag.model_dump(mode="json"), "escalate_reason": None,
               "trail": await mark("investigate", note)}
        if diag.root_cause is None:
            out["escalate_reason"] = "No likely cause found in the evidence. Waking a human with everything collected."
        return out

    async def plan_node(state):
        diag = Diagnosis(**state["diagnosis"])
        ruled, loops = state.get("ruled_out", []), state.get("loops", 0)
        rules = _rules(d)
        proposal, cause_id = None, None
        if loops == 0 and diag.root_cause:
            seen = recurrence.count_recent(d.history(), diag.root_cause.service,
                                           window_s=rules.get("recurrence_window_seconds", 600)) + 1
            if seen >= rules.get("recurrence_threshold", 3):
                inc["recurring"] = seen
                mins = rules.get("recurrence_window_seconds", 600) // 60
                proposal, reversing = recurrence.reverse_proposal(diag, d.known_good)
                if proposal:
                    cause_id = diag.root_cause.id
                    await d.step(inc, "recurring", "reverse",
                                 f"{diag.root_cause.service} has failed {seen} times in {mins} min. Fixing the symptom "
                                 f"again isn't enough: reversing the latest change ({reversing['id']} by "
                                 f"{reversing['actor']}) to the known-good version.")
                else:
                    await d.step(inc, "recurring", "alert",
                                 f"{diag.root_cause.service} has failed {seen} times in {mins} min and there is no recent "
                                 "change to reverse. Fixing it again now; an engineer must find the permanent fix.")
                d.security_event("recurring_problem", f"{diag.root_cause.service} failed {seen} times in {mins} min",
                                 {"incident": inc["id"]})
        if proposal is None:
            for hyp in [diag.root_cause] + list(diag.alternatives or []):
                if hyp is None or hyp.id in ruled:
                    continue
                p = plan_fix(diag.model_copy(update={"root_cause": hyp}), d.known_good)
                if p:
                    proposal, cause_id = p, hyp.id
                    break
        if proposal is None:
            return {"escalate_reason": "No action in the catalogue fits this cause. Waking a human.",
                    "trail": await mark("plan", "no safe fix in the catalogue")}
        inc["proposal"] = proposal.model_dump(mode="json")
        await d.step(inc, "planned", "done", ("Proposed " if loops == 0 else "Trying the next fix: ")
                     + f"{proposal.operation} on {proposal.target}", inc["proposal"])
        return {"proposal": proposal.model_dump(mode="json"), "cause_id": cause_id, "escalate_reason": None,
                "trail": await mark("plan", f"{proposal.operation} {proposal.target}")}

    async def practise(state):
        proposal = ActionProposal(**state["proposal"])
        await d.step(inc, "simulation", "running", "Rehearsing the fix on the shadow copy: reproduce first, then fix")
        inc["sim_live"] = []
        inc.pop("simulation", None)

        def on_sim_step(s):
            inc["sim_live"].append(s)
            ping()

        sim = await asyncio.to_thread(d.simulator.simulate, proposal, on_sim_step)
        proposal.parameters["simulation"] = sim
        inc["proposal"] = proposal.model_dump(mode="json")
        inc["simulation"] = sim
        d.on_shadow({"incident": inc["id"], "status": sim["status"], "duration_s": sim["duration_s"]})
        label = {"passed": "passed", "failed": "failed", "not_reproducible": "not reproducible"}.get(sim["status"], "error")
        summary = " | ".join(f"{s['stage']}: {s['detail']}" for s in sim["steps"] if s["stage"] != "restore")
        await d.step(inc, "simulation", label, f"{summary} ({sim['duration_s']}s)", sim)
        return {"proposal": proposal.model_dump(mode="json"), "simulation": sim,
                "trail": await mark("practise", f"shadow copy: {label}")}

    async def rethink(state):
        ruled = list(state.get("ruled_out", [])) + ([state["cause_id"]] if state.get("cause_id") else [])
        loops = state.get("loops", 0) + 1
        real_shop = state.get("outcome") == "failed"
        why = "The fix did not hold on the real shop (it was undone)" if real_shop else "The fix did not work on the shadow copy"
        if state.get("memory_hit"):
            d.memory.forget(state["fingerprint"])  # the remembered fix was wrong this time
        diag = Diagnosis(**state["diagnosis"])
        left = [h for h in [diag.root_cause] + list(diag.alternatives or []) if h and h.id not in ruled]
        out = {"ruled_out": ruled, "loops": loops, "memory_hit": None, "outcome": None, "escalate_reason": None}
        if loops > _rules(d).get("max_rethinks", 2) or not left:
            out["escalate_reason"] = (
                (f"Not recovered after {state.get('verify_seconds')}s. The system was left as the agent found it. "
                 "Waking a human." if real_shop else
                 "Every fix the agent could find failed its practice run. Nothing was changed on the real shop. "
                 "Waking a human."))
            out["trail"] = await mark("rethink", "out of ideas: handing over")
            return out
        await d.step(inc, "rethink", "running",
                     f"{why}, so {state.get('cause_id')} is ruled out. Thinking again with that new clue "
                     f"(attempt {loops + 1}).")
        out["trail"] = await mark("rethink", f"ruled out {state.get('cause_id')}")
        return out

    async def safety_node(state):
        proposal = ActionProposal(**state["proposal"])
        human_approval = _rules(d).get("human_approval", False)
        decision = d.safety.decide(proposal, kill_switch=d.kill_switch(),
                                   always_ask=human_approval and d.approval_mode() == "always")
        inc["decision"] = decision.model_dump(mode="json")
        engineer = any(r.startswith("engineer approval required") for r in decision.reasons)
        shown = ("NEEDS ENGINEER" if engineer and decision.verdict == "APPROVAL_REQUIRED"
                 else "ALLOW (reversible)" if decision.verdict == "APPROVAL_REQUIRED" and not human_approval
                 else decision.verdict)
        inc["decision"]["shown_verdict"] = shown
        await d.step(inc, "policy", shown, "; ".join(decision.reasons), inc["decision"])
        if decision.verdict == "BLOCK":
            return {"decision": decision.model_dump(mode="json"), "outcome": "blocked",
                    "escalate_reason": "Action blocked by policy. A human must take over.",
                    "trail": await mark("safety", "blocked by policy")}
        if decision.verdict == "APPROVAL_REQUIRED" and not human_approval and not engineer:
            why = "; ".join(r for r in decision.reasons if "allowed by policy" not in r and not r.startswith("rehearsed"))
            decision = d.safety.approve(proposal, decision, "the agent: reversible fix, auto-undo armed")
            decision.verdict = "ALLOW"
            inc["decision"] = decision.model_dump(mode="json")
            inc["decision"]["shown_verdict"] = "ALLOW (reversible)"
            await d.step(inc, "proceeding", "reversible",
                         f"Not fully proven ({why}), but the fix is reversible: applying it with auto-undo armed. "
                         "Engineer notified.")
        if decision.verdict == "APPROVAL_REQUIRED":
            rules = _rules(d)
            auto_ok = (not engineer and d.approval_mode() == "timed"
                       and d.safety.decide(proposal, kill_switch=d.kill_switch()).verdict == "ALLOW")
            reason = next((r.split(": ", 1)[1] for r in decision.reasons if r.startswith("engineer approval required")),
                          "this fix needs a human decision")
            request = {"action_id": proposal.action_id, "engineer": engineer, "reason": reason, "auto_ok": auto_ok,
                       "timeout": rules.get("auto_approve_after_seconds", 20) if auto_ok
                       else rules.get("approval_timeout_seconds", 120)}
            return {"decision": decision.model_dump(mode="json"), "approval_request": request,
                    "trail": await mark("safety", "risky: asking the on-call engineer")}
        return {"decision": decision.model_dump(mode="json"), "approval_request": None,
                "trail": await mark("safety", "allowed: signed one-time permission issued")}

    async def ask_engineer(state):
        # LangGraph human-in-the-loop: the graph pauses here and is saved. It resumes with the engineer's answer.
        answer = interrupt(state["approval_request"])
        approved, who = bool(answer.get("approved")), str(answer.get("who", "unknown"))
        if not approved:
            await d.step(inc, "approval", "denied", f"Not approved ({who}). Nothing was changed.")
            return {"approval": answer, "outcome": "denied", "escalate_reason": None,
                    "trail": await mark("ask_engineer", f"not approved ({who})")}
        proposal = ActionProposal(**state["proposal"])
        decision = d.safety.approve(proposal, PolicyDecision(**state["decision"]), who)
        inc["decision"] = decision.model_dump(mode="json")
        await d.step(inc, "approval", "approved",
                     f"Approved by {who}" if not who.startswith("auto-approved") else who[0].upper() + who[1:])
        return {"approval": answer, "decision": decision.model_dump(mode="json"),
                "trail": await mark("ask_engineer", f"approved by {who}")}

    async def execute(state):
        proposal, decision = ActionProposal(**state["proposal"]), PolicyDecision(**state["decision"])
        inc["status"] = "executing"
        await d.step(inc, "executing", "running", f"{proposal.operation} {proposal.target}")
        result = await asyncio.to_thread(d.executor.execute, proposal, decision)
        d.safety.record_execution(proposal)
        inc["result"] = result
        await d.step(inc, "executed", "done" if result["ok"] else "failed", result["detail"], result)
        out = {"result": result, "trail": await mark("execute", "done" if result["ok"] else "failed")}
        if not result["ok"]:
            out["escalate_reason"] = "Execution failed. Waking a human."
        return out

    async def verify_node(state):
        proposal = ActionProposal(**state["proposal"])
        await d.step(inc, "verifying", "running", f"Watching {', '.join(proposal.verify_services)} and a real checkout")
        ok, last, took = await d.verify(proposal.verify_services)
        if ok:
            inc["status"] = "resolved"
            inc["recovery_seconds"] = round(time.time() - inc["started"], 1)
            await d.step(inc, "resolved", "done",
                         f"Recovered. Verified in {took}s. Time to recovery {inc['recovery_seconds']}s. "
                         f"Touched only: {proposal.target}.")
            if proposal.parameters.get("service") == "payment":
                try:
                    fu = await asyncio.to_thread(d.money_follow_up, inc, proposal)
                except Exception as e:  # the fix worked; a failed follow-up check must not undo that
                    fu = None
                    print("money follow-up check failed:", e)
                if fu:
                    inc["follow_up"] = fu
                    await d.step(inc, "follow_up", "human", fu["detail"], fu["counts"])
            outcome = "resolved"
        elif judge_partial(last, proposal.parameters.get("service")) == "partial":
            inc["status"] = "partially_resolved"
            inc["partial"] = True
            inc["recovery_seconds"] = round(time.time() - inc["started"], 1)
            await d.step(inc, "partial", "done",
                         f"{proposal.parameters.get('service')} is fixed, but customers are still affected: another "
                         "problem remains. Keeping this fix and going after the next cause now.")
            outcome = "partial"
        else:
            outcome = "failed"
        return {"outcome": outcome, "verify_seconds": took, "trail": await mark("verify", outcome)}

    async def undo(state):
        undo_info = (state.get("result") or {}).get("undo")
        if undo_info and _rules(d).get("auto_undo_on_failed_verification", True):
            await d.step(inc, "auto_undo", "running",
                         f"The fix did not hold after {state.get('verify_seconds')}s: reversing it ({undo_info['describe']})")
            r = await asyncio.to_thread(d.run_undo, inc, undo_info, "auto")
            await d.step(inc, "auto_undo", "done" if r["ok"] else "failed", r["detail"])
            inc["undone"] = r["ok"]
            note = "reversed from the snapshot" if r["ok"] else "undo failed"
        else:
            note = "nothing to undo"
        return {"outcome": "failed", "trail": await mark("undo", note)}

    async def learn(state):
        diag = Diagnosis(**state["diagnosis"])
        proposal = ActionProposal(**state["proposal"])
        note = "partly fixed: not saved to memory"
        if state.get("outcome") == "resolved" and diag.root_cause:
            item = d.memory.remember(state["fingerprint"], state.get("fingerprint_text", ""),
                                     {"kind": diag.root_cause.kind, "service": diag.root_cause.service},
                                     proposal.operation, inc["id"], inc.get("recovery_seconds"), d.budget.used)
            inc["agent"]["memory"]["saved"] = True
            note = f"saved to memory (fixed {item['times_fixed']}x): next time 0 AI tokens"
            await d.step(inc, "learned", "done", "Saved to memory: if this exact problem happens again, the agent "
                                                 "already knows the fix and uses 0 AI tokens. Postmortem written.")
        trail = await mark("learn", note)
        inc["agent"]["postmortem"] = build_postmortem(inc, d.budget.public())
        return {"trail": trail}

    async def escalate(state):
        if state.get("escalate_reason"):
            await d.step(inc, "escalated", "done", state["escalate_reason"])
        if inc.get("status") not in ("resolved", "partially_resolved"):
            inc["status"] = "escalated"
        trail = await mark("escalate", state.get("escalate_reason") or "handed to a human")
        inc["agent"]["postmortem"] = build_postmortem(inc, d.budget.public())
        return {"trail": trail}

    # ---- wiring --------------------------------------------------------------------------------
    g = StateGraph(IncidentState)
    for name, fn in [("triage", triage), ("recall", recall), ("investigate", investigate_node), ("plan", plan_node),
                     ("practise", practise), ("rethink", rethink), ("safety", safety_node),
                     ("ask_engineer", ask_engineer), ("execute", execute), ("verify", verify_node), ("undo", undo),
                     ("learn", learn), ("escalate", escalate)]:
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", lambda s: "recall" if s.get("memory_hit") else "investigate",
                            {"recall": "recall", "investigate": "investigate"})
    g.add_conditional_edges("recall", lambda s: "plan" if s.get("memory_hit") else "investigate",
                            {"plan": "plan", "investigate": "investigate"})
    g.add_conditional_edges("investigate", lambda s: "escalate" if s.get("escalate_reason") else "plan",
                            {"plan": "plan", "escalate": "escalate"})
    g.add_conditional_edges("plan", lambda s: "escalate" if s.get("escalate_reason") else "practise",
                            {"practise": "practise", "escalate": "escalate"})
    g.add_conditional_edges("practise", lambda s: "rethink" if s["simulation"]["status"] == "failed" else "safety",
                            {"rethink": "rethink", "safety": "safety"})
    g.add_conditional_edges("rethink", lambda s: "escalate" if s.get("escalate_reason") else "investigate",
                            {"investigate": "investigate", "escalate": "escalate"})
    g.add_conditional_edges("safety", lambda s: ("escalate" if s.get("outcome") == "blocked"
                                                 else "ask_engineer" if s.get("approval_request") else "execute"),
                            {"escalate": "escalate", "ask_engineer": "ask_engineer", "execute": "execute"})
    g.add_conditional_edges("ask_engineer", lambda s: "escalate" if s.get("outcome") == "denied" else "execute",
                            {"escalate": "escalate", "execute": "execute"})
    g.add_conditional_edges("execute", lambda s: "escalate" if s.get("escalate_reason") else "verify",
                            {"verify": "verify", "escalate": "escalate"})
    g.add_conditional_edges("verify", lambda s: "undo" if s.get("outcome") == "failed" else "learn",
                            {"undo": "undo", "learn": "learn"})
    g.add_edge("undo", "rethink")
    g.add_edge("learn", END)
    g.add_edge("escalate", END)
    return g.compile(checkpointer=MemorySaver())


async def run_incident(d: AgentDeps, event, snap):
    """Runs one incident through the graph. Pauses for the engineer when the graph asks (interrupt)."""
    d.inc.setdefault("agent", {})
    d.inc["agent"].update({"engine": ENGINE, "trail": [], "investigations": [], "tokens": d.budget.public(),
                           "memory": {}, "postmortem": None})
    graph = build_graph(d)
    cfg = {"configurable": {"thread_id": d.inc["id"]}, "recursion_limit": 80}
    out = await graph.ainvoke({"incident_id": d.inc["id"], "event": event.model_dump(mode="json"), "snap": snap,
                               "loops": 0, "ruled_out": [], "trail": []}, cfg)
    while True:
        request = None
        if isinstance(out, dict) and out.get("__interrupt__"):
            request = out.get("approval_request")
            if request is None:
                first = out["__interrupt__"][0]
                request = getattr(first, "value", None)
        else:
            snapshot = await graph.aget_state(cfg)
            if snapshot.next:
                request = snapshot.values.get("approval_request")
        if not request:
            break
        approved, who = await d.wait_for_approval(d.inc, request)
        out = await graph.ainvoke(Command(resume={"approved": approved, "who": who}), cfg)
    d.inc["agent"]["tokens"] = d.budget.public()
    return out
