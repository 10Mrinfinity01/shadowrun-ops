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
from agent.contracts import ActionProposal, Grant
from agent.execution.executor import Executor
from agent.investigation.investigator import Context, investigate
from agent.llm import chat_json
from agent.monitoring.monitor import Monitor
from agent.planner.planner import plan
from agent.safety.engine import SafetyEngine
from agent.shadow.simulator import ShadowSimulator
from agent.verification.verifier import verify

AUDIT_FILE = changes.DATA / "audit.jsonl"
HISTORY_FILE = changes.DATA / "history.json"

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


def effective_changes():
    """Recent human changes that are still running (not already rolled back), latest per service + kind."""
    latest = {}
    for c in changes.recent_changes():
        latest[(c["service"], c["kind"])] = c
    return [c for c in latest.values() if changes.read_runtime(c["service"], c["kind"]) == c["after"]]


def errors_since(container, since):
    return sum(1 for line in monitor.logs(container, lines=300, since=since) if " ERROR " in f" {line} ")


def build_context():
    return Context(read_logs=monitor.logs, errors_since=errors_since, recent_changes=effective_changes())


async def step(inc, stage, status, detail, data=None):
    inc["steps"].append({"time": now_str(), "stage": stage, "status": status, "detail": detail, "data": data})
    audit("step", {"incident": inc["id"], "stage": stage, "status": status, "detail": detail, "data": data})
    await broadcast()


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

        proposal = plan(diagnosis, known_good)
        if proposal is None:
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", "No safe action in the catalogue for this cause. Waking a human.")
            return
        inc["proposal"] = proposal.model_dump(mode="json")
        await step(inc, "planned", "done", f"Proposed {proposal.operation} on {proposal.target}", inc["proposal"])

        await step(inc, "simulation", "running", "Rehearsing the fix on the shadow copy: reproduce first, then fix")
        sim = await asyncio.to_thread(simulator.simulate, proposal)
        proposal.parameters["simulation"] = sim
        inc["proposal"] = proposal.model_dump(mode="json")
        inc["simulation"] = sim
        STATE["shadow"]["last"] = {"incident": inc["id"], "status": sim["status"], "duration_s": sim["duration_s"]}
        label = {"passed": "passed", "failed": "failed", "not_reproducible": "not reproducible"}.get(sim["status"], "error")
        summary = " | ".join(f"{s['stage']}: {s['detail']}" for s in sim["steps"] if s["stage"] != "restore")
        await step(inc, "simulation", label, f"{summary} ({sim['duration_s']}s)", sim)

        decision = safety.decide(proposal, kill_switch=STATE["kill_switch"])
        inc["decision"] = decision.model_dump(mode="json")
        await step(inc, "policy", decision.verdict, "; ".join(decision.reasons), inc["decision"])

        if decision.verdict == "BLOCK":
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", "Action blocked by policy. A human must take over.")
            return

        if decision.verdict == "APPROVAL_REQUIRED":
            inc["status"] = "awaiting_approval"
            fut = asyncio.get_running_loop().create_future()
            APPROVALS[proposal.action_id] = fut
            timeout = safety.policy["rules"]["approval_timeout_seconds"]
            await step(inc, "awaiting_approval", "running", f"Waiting up to {timeout}s for a human. No answer means no.")
            try:
                approved, who = await asyncio.wait_for(fut, timeout=timeout)
            except asyncio.TimeoutError:
                approved, who = False, "timeout"
            finally:
                APPROVALS.pop(proposal.action_id, None)
            if not approved:
                inc["status"] = "escalated"
                await step(inc, "approval", "denied", f"Not approved ({who}). Nothing was changed.")
                return
            decision = safety.approve(proposal, decision, who)
            inc["decision"] = decision.model_dump(mode="json")
            await step(inc, "approval", "approved", f"Approved by {who}")

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
        else:
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", f"Not recovered after {took}s. Waking a human.")
    except Exception as e:
        inc["status"] = "error"
        await step(inc, "error", "failed", f"Agent error: {e}")
    finally:
        audit("incident_closed", {"incident": inc["id"], "status": inc["status"],
                                  "recovery_seconds": inc["recovery_seconds"]})
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
            if not monitor.is_healthy(snap):
                STATE["last_unhealthy_at"] = time.time()
            STATE["deploy_window"] = changes.in_deploy_window()
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
