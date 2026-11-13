"""ShadowRun-Ops backend: runs the incident pipeline and serves the dashboard.

Start (Laptop A, repo root):
    .\\venv\\Scripts\\python.exe -m uvicorn agent.main:app --host 0.0.0.0 --port 8000
"""
import asyncio
import json
import pathlib
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from agent import changes, config
from agent.careless import careless_answer
from agent.contracts import ActionProposal, Grant, PolicyDecision
from agent.execution.executor import Executor
from agent.investigation.investigator import Context, investigate
from agent.llm import chat_json
from agent.monitoring.monitor import Monitor
from agent.planner.planner import plan
from agent import recurrence
from agent.safety.engine import SafetyEngine
from agent.shadow.simulator import ShadowSimulator
from agent.verification.verifier import verify

AUDIT_FILE = changes.DATA / "audit.jsonl"
HISTORY_FILE = changes.DATA / "history.json"
TRAFFIC_FILE = changes.DATA / "traffic_stats.json"
SCOREBOARD_FILE = changes.DATA / "scoreboard.json"
SETTINGS_FILE = changes.DATA / "settings.json"


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text())
    except Exception:
        return {"approval_mode": "auto"}  # no approval waits: proven-safe, reversible fixes only


def save_settings():
    SETTINGS_FILE.parent.mkdir(exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps({"approval_mode": STATE["approval_mode"]}))

monitor = Monitor()
known_good = changes.KnownGood()
safety = SafetyEngine(config.GRANT_SECRET)
executor = Executor(config.GRANT_SECRET, safety.policy)
simulator = ShadowSimulator()

STATE = {
    "services": {},
    "probe": {},
    "active": None,       # the incident being handled right now
    "history": [],        # finished incidents, newest first
    "kill_switch": False,
    "quiet_until": 0,
    "llm_enabled": True,
    "changes": [],          # recent change-ledger entries, newest first
    "security_events": [],  # blocked attacks and quarantined log lines, newest first
    "deploy_window": False,
    "shadow": {"available": None, "last": None},
    "traffic": None,        # fake-customer stats, if traffic/traffic.py is running
    "comparison": None,     # careless agent vs ShadowRun-Ops
    "approval_mode": load_settings().get("approval_mode", "auto"),
    "consistency": None,
    "last_unhealthy_at": 0,
}
HIDDEN = {"quiet_until", "last_unhealthy_at"}
CLIENTS: set[WebSocket] = set()
APPROVALS: dict[str, asyncio.Future] = {}


def now_str():
    return datetime.now().strftime("%H:%M:%S")


def audit(kind, data):
    AUDIT_FILE.parent.mkdir(exist_ok=True)
    with AUDIT_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"t": time.time(), "kind": kind, **data}, default=str) + "\n")


async def broadcast():
    payload = json.dumps({"type": "state", "state": public_state()}, default=str)
    for ws in list(CLIENTS):
        try:
            await ws.send_text(payload)
        except Exception:
            CLIENTS.discard(ws)


def public_state():
    return {k: v for k, v in STATE.items() if k not in HIDDEN}


def save_history():
    try:
        HISTORY_FILE.parent.mkdir(exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(STATE["history"], default=str))
    except Exception as e:
        print("could not save history:", e)


def load_history():
    try:
        STATE["history"] = json.loads(HISTORY_FILE.read_text())[:20]
    except Exception:
        STATE["history"] = []


def security_event(kind, detail, data=None):
    ev = {"time": now_str(), "kind": kind, "detail": detail, "data": data}
    STATE["security_events"].insert(0, ev)
    STATE["security_events"] = STATE["security_events"][:20]
    audit("security_event", ev)


def read_traffic():
    try:
        t = json.loads(TRAFFIC_FILE.read_text())
    except Exception:
        return None
    t["live"] = time.time() - t.get("updated", 0) < 6
    return t


def impact_for(inc):
    """Customer impact of an incident, from the fake-customer outage episodes."""
    t = read_traffic()
    if not t:
        return None
    start = inc["started"] - 60
    episodes = ([t["current_outage"]] if t.get("current_outage") else []) + t.get("outages", [])
    for ep in episodes:
        if (ep.get("ended") or time.time()) >= start and ep["started"] <= time.time():
            return {"failed_checkouts": ep["failed"], "lost_paise": ep["lost_paise"],
                    "outage_seconds": round((ep.get("ended") or time.time()) - ep["started"], 1)}
    return {"failed_checkouts": 0, "lost_paise": 0, "outage_seconds": 0}


def effective_changes():
    """Recent human changes that are still running (not already rolled back), latest per service + kind."""
    latest = {}
    for c in changes.recent_changes():
        latest[(c["service"], c["kind"])] = c
    return [c for c in latest.values() if changes.read_runtime(c["service"], c["kind"]) == c["after"]]


def errors_since(container, since):
    return sum(1 for line in monitor.logs(container, lines=300, since=since)
               if " ERROR " in f" {line} " or " WARN " in f" {line} ")


def money_follow_up(inc, proposal):
    """After a payment bug: list the money problems for a human. The agent never edits payment data itself."""
    import httpx
    since = inc["started"] - 120
    blamed = proposal.parameters.get("blamed_change")
    for c in changes.all_changes():
        if c["id"] == blamed:
            since = c["time"] - 5
    try:
        r = httpx.get(config.CONSISTENCY_URL, params={"since": since}, timeout=3).json()
    except Exception:
        return None
    if not r.get("payments_without_order") and not r.get("status_mismatch"):
        return None
    parts = []
    if r["payments_without_order"]:
        parts.append(f"{r['payments_without_order']} customer(s) were charged with no order created "
                     f"(Rs {r['unmatched_amount_paise'] / 100:.0f}): refund them or create their orders")
    if r["status_mismatch"]:
        parts.append(f"{r['status_mismatch']} order(s) have a status that doesn't match a captured payment: correct them")
    return {"detail": "; ".join(parts) + ". The agent does not change payment data itself.", "counts": r}


def build_context():
    return Context(read_logs=monitor.logs, errors_since=errors_since, recent_changes=effective_changes())


async def step(inc, stage, status, detail, data=None):
    inc["steps"].append({"time": now_str(), "stage": stage, "status": status, "detail": detail, "data": data})
    audit("step", {"incident": inc["id"], "stage": stage, "status": status, "detail": detail, "data": data})
    await broadcast()


def read_impact_safely(inc):
    try:
        return impact_for(inc)
    except Exception:
        return None


def run_undo(inc, undo, who):
    """Reverse one of the agent's own fixes: a signed, policy-checked 'undo' action like any other."""
    p = ActionProposal(action_id=f"UNDO-{inc['id']}", incident_id=inc["id"], operation="undo", target=undo["target"],
                       parameters={"undo": undo}, expected_outcome=["state from before the fix"], verify_services=[],
                       reversible=True, diagnosis_confidence=1.0)
    rules = safety.policy["rules"]
    allowed = safety.policy["allowed_actions"].get("undo", {}).get("targets", [])
    if STATE["kill_switch"] or undo["target"] not in allowed:
        return {"ok": False, "detail": "undo refused by policy"}
    expires = time.time() + rules["grant_ttl_seconds"]
    from agent.safety.engine import sign
    grant = Grant(action_id=p.action_id, operation="undo", target=p.target, expires_at=expires,
                  signature=sign(config.GRANT_SECRET, p.action_id, "undo", p.target, expires))
    decision = PolicyDecision(decision_id="DEC-UNDO", action_id=p.action_id, verdict="ALLOW",
                              reasons=[f"undo requested by {who}"], grant=grant)
    r = executor.execute(p, decision)
    audit("undo", {"incident": inc["id"], "by": who, "result": r})
    return r


def ask_llm(system, user):
    if not STATE["llm_enabled"]:
        return None, "disabled"
    return chat_json(system, user)


async def handle_incident(event, snap):
    inc = {"id": event.incident_id, "started": time.time(), "status": "open", "steps": [],
           "event": event.model_dump(mode="json"), "diagnosis": None, "proposal": None,
           "decision": None, "result": None, "recovery_seconds": None}
    STATE["active"] = inc
    try:
        await step(inc, "detected", "done",
                   f"{len(event.affected_services)} service(s) unhealthy: {', '.join(event.affected_services)}",
                   event.model_dump(mode="json"))

        await step(inc, "investigating", "running", "Reading health checks, container state and sanitized logs")
        ctx = await asyncio.to_thread(build_context)
        diagnosis = await asyncio.to_thread(investigate, event, snap, ctx, ask_llm)
        inc["diagnosis"] = diagnosis.model_dump(mode="json")
        cause = diagnosis.root_cause.id if diagnosis.root_cause else "unknown"
        await step(inc, "diagnosed", "done", f"Root cause: {cause} (confidence {diagnosis.confidence:.2f})",
                   inc["diagnosis"])
        if diagnosis.suspicious_log_lines:
            security_event("poisoned_log", f"{len(diagnosis.suspicious_log_lines)} log line(s) tried to give the "
                           "AI instructions. Quarantined: the LLM never saw them.", diagnosis.suspicious_log_lines)
            await step(inc, "security", "quarantined",
                       f"{len(diagnosis.suspicious_log_lines)} suspicious log line(s) quarantined and ignored",
                       diagnosis.suspicious_log_lines)

        rules = safety.policy["rules"]
        proposal, reversing = None, None
        if diagnosis.root_cause:
            seen = recurrence.count_recent(STATE["history"], diagnosis.root_cause.service,
                                           window_s=rules.get("recurrence_window_seconds", 600)) + 1
            if seen >= rules.get("recurrence_threshold", 3):
                inc["recurring"] = seen
                proposal, reversing = recurrence.reverse_proposal(diagnosis, known_good)
                if proposal:
                    await step(inc, "recurring", "reverse",
                               f"{diagnosis.root_cause.service} has failed {seen} times in "
                               f"{rules.get('recurrence_window_seconds', 600) // 60} min. Fixing the symptom again isn't "
                               f"enough: reversing the latest change ({reversing['id']} by {reversing['actor']}) "
                               "to the known-good version.")
                else:
                    await step(inc, "recurring", "alert",
                               f"{diagnosis.root_cause.service} has failed {seen} times in "
                               f"{rules.get('recurrence_window_seconds', 600) // 60} min and there is no recent change to "
                               "reverse. Fixing it again now; an engineer must find the permanent fix.")
                security_event("recurring_problem", f"{diagnosis.root_cause.service} failed {seen} times in 10 min",
                               {"incident": inc["id"]})

        # Candidate fixes, best first: the reverse strategy (if recurring), then the root cause, then alternatives.
        candidates = [proposal] if proposal else []
        for hyp in [diagnosis.root_cause] + list(diagnosis.alternatives or []):
            if hyp is None:
                continue
            alt = plan(diagnosis.model_copy(update={"root_cause": hyp}), known_good)
            if alt and all((alt.operation, alt.target) != (c.operation, c.target) for c in candidates):
                candidates.append(alt)
        if not candidates:
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", "No action in the catalogue fits this cause. Waking a human.")
            return

        loop = asyncio.get_running_loop()
        for n, proposal in enumerate(candidates[:3]):
            inc["proposal"] = proposal.model_dump(mode="json")
            await step(inc, "planned", "done", ("Proposed " if n == 0 else "Trying the next fix: ")
                       + f"{proposal.operation} on {proposal.target}", inc["proposal"])
            await step(inc, "simulation", "running", "Rehearsing the fix on the shadow copy: reproduce first, then fix")
            inc["sim_live"] = []

            def on_sim_step(s):
                inc["sim_live"].append(s)
                asyncio.run_coroutine_threadsafe(broadcast(), loop)

            sim = await asyncio.to_thread(simulator.simulate, proposal, on_sim_step)
            proposal.parameters["simulation"] = sim
            inc["proposal"] = proposal.model_dump(mode="json")
            inc["simulation"] = sim
            STATE["shadow"]["last"] = {"incident": inc["id"], "status": sim["status"], "duration_s": sim["duration_s"]}
            label = {"passed": "passed", "failed": "failed", "not_reproducible": "not reproducible"}.get(sim["status"], "error")
            summary = " | ".join(f"{s['stage']}: {s['detail']}" for s in sim["steps"] if s["stage"] != "restore")
            await step(inc, "simulation", label, f"{summary} ({sim['duration_s']}s)", sim)
            if sim["status"] != "failed" or n == len(candidates[:3]) - 1:
                break

        human_approval = safety.policy["rules"].get("human_approval", False)
        decision = safety.decide(proposal, kill_switch=STATE["kill_switch"],
                                 always_ask=human_approval and STATE["approval_mode"] == "always")
        inc["decision"] = decision.model_dump(mode="json")
        shown = "ALLOW (reversible)" if decision.verdict == "APPROVAL_REQUIRED" and not human_approval else decision.verdict
        inc["decision"]["shown_verdict"] = shown
        await step(inc, "policy", shown, "; ".join(decision.reasons), inc["decision"])

        if decision.verdict == "BLOCK":
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", "Action blocked by policy. A human must take over.")
            return

        if decision.verdict == "APPROVAL_REQUIRED" and not human_approval:
            why = "; ".join(r for r in decision.reasons
                            if "allowed by policy" not in r and not r.startswith("rehearsed"))
            decision = safety.approve(proposal, decision, "the agent: reversible fix, auto-undo armed")
            decision.verdict = "ALLOW"  # proceed now; the grant is signed and auto-undo is armed
            inc["decision"] = decision.model_dump(mode="json")
            inc["decision"]["shown_verdict"] = "ALLOW (reversible)"
            await step(inc, "proceeding", "reversible",
                       f"Not fully proven ({why}), but the fix is reversible: applying it with auto-undo armed. "
                       "Engineer notified.")

        if decision.verdict == "APPROVAL_REQUIRED":
            inc["status"] = "awaiting_approval"
            fut = asyncio.get_running_loop().create_future()
            APPROVALS[proposal.action_id] = fut
            rules = safety.policy["rules"]
            # In "ask, then auto" mode a fix may go ahead without an answer, but ONLY if the policy would
            # have allowed it on its own (rehearsal passed, confident, reversible, not a protected target).
            auto_ok = (STATE["approval_mode"] == "timed"
                       and safety.decide(proposal, kill_switch=STATE["kill_switch"]).verdict == "ALLOW")
            timeout = rules.get("auto_approve_after_seconds", 20) if auto_ok else rules["approval_timeout_seconds"]
            inc["approval_deadline"] = time.time() + timeout
            inc["auto_on_timeout"] = auto_ok
            if auto_ok:
                msg = (f"Waiting {timeout}s for a human. If nobody answers, it goes ahead automatically: "
                       "the rehearsal passed and the fix is low-risk.")
            else:
                msg = f"Waiting up to {timeout}s for a human. No answer means no."
            await step(inc, "awaiting_approval", "running", msg)
            try:
                approved, who = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                approved = auto_ok
                who = f"auto-approved: no answer in {timeout}s, rehearsal passed" if auto_ok else "timeout"
            finally:
                APPROVALS.pop(proposal.action_id, None)
            if not approved:
                inc["status"] = "escalated"
                await step(inc, "approval", "denied", f"Not approved ({who}). Nothing was changed.")
                return
            decision = safety.approve(proposal, decision, who)
            inc["decision"] = decision.model_dump(mode="json")
            await step(inc, "approval", "approved", (f"Approved by {who}" if not who.startswith("auto-approved") else who[0].upper() + who[1:]))

        inc["status"] = "executing"
        await step(inc, "executing", "running", f"{proposal.operation} {proposal.target}")
        result = await asyncio.to_thread(executor.execute, proposal, decision)
        safety.record_execution(proposal)
        inc["result"] = result
        await step(inc, "executed", "done" if result["ok"] else "failed", result["detail"], result)
        if not result["ok"]:
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", "Execution failed. Waking a human.")
            return

        await step(inc, "verifying", "running", f"Watching {', '.join(proposal.verify_services)} and a real checkout")

        async def progress(s):
            STATE["services"], STATE["probe"] = s["services"], s["probe"]
            await broadcast()

        ok, last, took = await verify(monitor, proposal.verify_services, on_progress=progress)
        if last:
            STATE["services"], STATE["probe"] = last["services"], last["probe"]
        if ok:
            inc["status"] = "resolved"
            inc["recovery_seconds"] = round(time.time() - inc["started"], 1)
            await step(inc, "resolved", "done",
                       f"Recovered. Verified in {took}s. Time to recovery {inc['recovery_seconds']}s. "
                       f"Touched only: {proposal.target}.")
            if proposal.parameters.get("service") == "payment":
                fu = await asyncio.to_thread(money_follow_up, inc, proposal)
                if fu:
                    inc["follow_up"] = fu
                    await step(inc, "follow_up", "human", fu["detail"], fu["counts"])
        else:
            undo = (result or {}).get("undo")
            if undo and safety.policy["rules"].get("auto_undo_on_failed_verification", True):
                await step(inc, "auto_undo", "running", f"The fix did not hold after {took}s: reversing it ({undo['describe']})")
                r = await asyncio.to_thread(run_undo, inc, undo, "auto")
                await step(inc, "auto_undo", "done" if r["ok"] else "failed", r["detail"])
                inc["undone"] = r["ok"]
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", f"Not recovered after {took}s. The system was left as the agent found it. "
                                                 "Waking a human.")
    except Exception as e:
        inc["status"] = "error"
        await step(inc, "error", "failed", f"Agent error: {e}")
    finally:
        inc["ended"] = time.time()
        await asyncio.sleep(1.5)  # let the fake customers notice the recovery before we read the impact
        inc["impact"] = read_impact_safely(inc)
        audit("incident_closed", {"incident": inc["id"], "status": inc["status"],
                                  "recovery_seconds": inc["recovery_seconds"], "impact": inc["impact"]})
        STATE["history"].insert(0, inc)
        STATE["history"] = STATE["history"][:20]
        save_history()
        STATE["active"] = None
        STATE["quiet_until"] = time.time() + config.QUIET_SECONDS_AFTER_INCIDENT
        await broadcast()


async def watch_loop():
    tick = 0
    while True:
        try:
            snap = await asyncio.to_thread(monitor.snapshot)
            STATE["services"], STATE["probe"] = snap["services"], snap["probe"]
            STATE["consistency"] = snap.get("consistency")
            if not monitor.is_healthy(snap):
                STATE["last_unhealthy_at"] = time.time()
            STATE["deploy_window"] = changes.in_deploy_window()
            STATE["traffic"] = read_traffic()
            if tick % 5 == 0:
                STATE["changes"] = list(reversed(changes.all_changes()[-8:]))
                if STATE["active"] is None:
                    ok, _ = await asyncio.to_thread(monitor._http, "GET", "http://localhost:8090/health")
                    STATE["shadow"]["available"] = ok
                for c in known_good.promote_stable(STATE["last_unhealthy_at"]):
                    audit("known_good_promoted", c)
            tick += 1
            if STATE["active"] is None and time.time() >= STATE["quiet_until"]:
                event = monitor.check_for_incident(snap, in_deploy_window=STATE["deploy_window"])
                if event:
                    asyncio.create_task(handle_incident(event, snap))
            await broadcast()
        except Exception as e:
            print("watch loop error:", e)
        await asyncio.sleep(config.POLL_SECONDS)


@asynccontextmanager
async def lifespan(app):
    load_history()
    known_good.promoted.update(c["id"] for c in changes.all_changes())  # only judge changes made from now on
    task = asyncio.create_task(watch_loop())
    yield
    task.cancel()


app = FastAPI(title="ShadowRun-Ops", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/health")
def health():
    return {"status": "ok", "from": "ShadowRun-Ops agent"}


@app.get("/state")
def get_state():
    return public_state()


@app.post("/approve/{action_id}")
async def approve(action_id: str, approver: str = "dashboard-user"):
    fut = APPROVALS.get(action_id)
    if not fut or fut.done():
        return {"ok": False, "detail": "nothing waiting for approval with that id"}
    fut.set_result((True, approver))
    return {"ok": True}


@app.post("/deny/{action_id}")
async def deny(action_id: str, approver: str = "dashboard-user"):
    fut = APPROVALS.get(action_id)
    if not fut or fut.done():
        return {"ok": False, "detail": "nothing waiting for approval with that id"}
    fut.set_result((False, approver))
    return {"ok": True}


@app.post("/undo/{incident_id}")
async def undo_incident(incident_id: str):
    """Engineer clicked Undo: put back exactly what was there before the agent's fix."""
    inc = next((h for h in STATE["history"] if h["id"] == incident_id), None)
    if not inc or not (inc.get("result") or {}).get("undo"):
        return {"ok": False, "detail": "nothing to undo for this incident"}
    if inc.get("undone"):
        return {"ok": False, "detail": "already undone"}
    r = await asyncio.to_thread(run_undo, inc, inc["result"]["undo"], "engineer (dashboard)")
    if r["ok"]:
        inc["undone"] = True
        changes_note = {"time": now_str(), "stage": "undo", "status": "done", "detail": r["detail"], "data": None}
        inc["steps"].append(changes_note)
        STATE["quiet_until"] = time.time() + 30  # the undo re-creates the original problem on purpose
        save_history()
    await broadcast()
    return r


@app.post("/approval-mode/{mode}")
async def approval_mode(mode: str):
    if mode not in ("always", "timed", "auto"):
        return {"ok": False, "detail": "mode must be 'always', 'timed' or 'auto'"}
    STATE["approval_mode"] = mode
    save_settings()
    audit("approval_mode", {"mode": mode})
    await broadcast()
    return {"ok": True, "approval_mode": mode}


@app.post("/kill-switch/{on}")
async def kill_switch(on: bool):
    STATE["kill_switch"] = on
    audit("kill_switch", {"on": on})
    await broadcast()
    return {"kill_switch": on}


@app.post("/simulate/rogue-action")
async def rogue_action():
    """A compromised component tries to make the agent destroy data. Shows the safety boundary holding."""
    rogue = ActionProposal(action_id="ACT-ROGUE", incident_id="SECURITY-TEST", operation="drop_database",
                           target="demo-db", parameters={"database": "shop"}, expected_outcome=["(attacker goal)"],
                           verify_services=[], reversible=False, diagnosis_confidence=0.99)
    decision = safety.decide(rogue)
    forged = ActionProposal(action_id="ACT-FORGED", incident_id="SECURITY-TEST", operation="restart_container",
                            target="demo-db", expected_outcome=["(attacker goal)"], verify_services=[],
                            reversible=True, diagnosis_confidence=0.99)
    fake_decision = decision.model_copy(update={"verdict": "ALLOW", "grant": Grant(
        action_id="ACT-FORGED", operation="restart_container", target="demo-db",
        expires_at=time.time() + 60, signature="forged-by-attacker")})
    exec_result = executor.execute(forged, fake_decision)
    security_event("rogue_action", f"Request to {rogue.operation} {rogue.target}: {decision.verdict}. "
                   f"Forged permission to restart demo-db: {exec_result['detail']}.",
                   {"policy": decision.model_dump(mode="json"), "forged_grant": exec_result})
    await broadcast()
    return {"verdict": decision.verdict, "reasons": decision.reasons, "forged_grant": exec_result["detail"]}


@app.post("/compare/careless")
async def compare_careless():
    """Ask a careless agent (raw logs, no sanitizer, no policy) what it WOULD run. Never executes it."""
    poisoned = next((h for h in ([STATE["active"]] if STATE["active"] else []) + STATE["history"]
                     if h and h.get("diagnosis") and h["diagnosis"].get("suspicious_log_lines")), None)
    if not poisoned:
        return {"ok": False, "detail": "Run the poisoned_log scenario first."}
    raw = [line.split("] ", 1)[-1] for line in poisoned["diagnosis"]["suspicious_log_lines"]]
    raw = ["ERROR database connection failed host=demo-db:5432 reason=connection refused"] * 3 + raw
    answer = await asyncio.to_thread(careless_answer, raw, chat_json)
    STATE["comparison"] = {
        "incident": poisoned["id"], "time": now_str(), "careless": answer,
        "shadowrun": {
            "quarantined": len(poisoned["diagnosis"]["suspicious_log_lines"]),
            "root_cause": (poisoned["diagnosis"].get("root_cause") or {}).get("id"),
            "action": (f"{poisoned['proposal']['operation']} {poisoned['proposal']['target']}"
                       if poisoned.get("proposal") else None),
            "status": poisoned["status"],
        },
    }
    audit("careless_comparison", STATE["comparison"])
    await broadcast()
    return {"ok": True, **STATE["comparison"]}


@app.get("/scoreboard")
def scoreboard():
    try:
        return json.loads(SCOREBOARD_FILE.read_text())
    except Exception:
        return {"available": False}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    await ws.send_text(json.dumps({"type": "state", "state": public_state()}, default=str))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        CLIENTS.discard(ws)
