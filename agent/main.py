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

from agent import config
from agent.execution.executor import Executor
from agent.investigation.investigator import investigate
from agent.llm import chat_json
from agent.monitoring.monitor import Monitor
from agent.planner.planner import plan
from agent.safety.engine import SafetyEngine
from agent.verification.verifier import verify

AUDIT_FILE = pathlib.Path("data/audit.jsonl")

monitor = Monitor()
safety = SafetyEngine(config.GRANT_SECRET)
executor = Executor(config.GRANT_SECRET, safety.policy)

STATE = {
    "services": {},
    "probe": {},
    "active": None,       # the incident being handled right now
    "history": [],        # finished incidents, newest first
    "kill_switch": False,
    "quiet_until": 0,
    "llm_enabled": True,
}
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
    return {k: v for k, v in STATE.items() if k != "quiet_until"}


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
        diagnosis = await asyncio.to_thread(investigate, event, snap, monitor.logs, ask_llm)
        inc["diagnosis"] = diagnosis.model_dump(mode="json")
        cause = diagnosis.root_cause.id if diagnosis.root_cause else "unknown"
        await step(inc, "diagnosed", "done", f"Root cause: {cause} (confidence {diagnosis.confidence:.2f})",
                   inc["diagnosis"])

        proposal = plan(diagnosis)
        if proposal is None:
            inc["status"] = "escalated"
            await step(inc, "escalated", "done", "No safe action in the catalogue for this cause. Waking a human.")
            return
        inc["proposal"] = proposal.model_dump(mode="json")
        await step(inc, "planned", "done", f"Proposed {proposal.operation} on {proposal.target}", inc["proposal"])

        await step(inc, "simulation", "skipped", "Shadow simulation arrives in Phase 2")

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
        STATE["active"] = None
        STATE["quiet_until"] = time.time() + config.QUIET_SECONDS_AFTER_INCIDENT
        await broadcast()


async def watch_loop():
    while True:
        try:
            snap = await asyncio.to_thread(monitor.snapshot)
            STATE["services"], STATE["probe"] = snap["services"], snap["probe"]
            if STATE["active"] is None and time.time() >= STATE["quiet_until"]:
                event = monitor.check_for_incident(snap)
                if event:
                    asyncio.create_task(handle_incident(event, snap))
            await broadcast()
        except Exception as e:
            print("watch loop error:", e)
        await asyncio.sleep(config.POLL_SECONDS)


@asynccontextmanager
async def lifespan(app):
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
