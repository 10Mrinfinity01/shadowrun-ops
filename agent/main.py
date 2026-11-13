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
from agent.investigation.investigator import Context
from agent.llm import chat_json
from agent.monitoring.monitor import Monitor
from agent.safety.engine import SafetyEngine
from agent.shadow.simulator import ShadowSimulator
from agent.verification.verifier import verify
from agent.agentic.engine import ENGINE
from agent.agentic.graph import EDGES, NODES, AgentDeps, run_incident
from agent.agentic.investigator_agent import TokenBudget
from agent.agentic.memory import IncidentMemory
from agent.agentic.observability import Observability

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
obs = Observability()
memory = IncidentMemory()
LATEST = {"snap": None}

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
    "observability": None,  # alerts, service map, metrics (always on, no AI)
    "agent_info": {"engine": ENGINE, "nodes": NODES, "edges": EDGES, "memory": memory.stats(),
                   "tokens_total": 0, "memory_hits": 0},
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


def ask_llm(system, user, prefer=None, timeout=8):
    if not STATE["llm_enabled"]:
        return None, "disabled"
    return chat_json(system, user, prefer=prefer, timeout=timeout)


def test_checkout():
    ok, detail, ms = monitor._timed_http("POST", config.CUSTOMER_PROBE_URL)
    return {"result": "ok" if ok else "failed", "detail": detail, "latency_ms": ms}


async def wait_for_approval(inc, request):
    """The graph paused at ask_engineer (LangGraph interrupt). Wait for Approve / Deny / timeout."""
    inc["status"] = "awaiting_approval"
    fut = asyncio.get_running_loop().create_future()
    APPROVALS[request["action_id"]] = fut
    timeout, auto_ok = request["timeout"], request["auto_ok"]
    inc["approval_deadline"] = time.time() + timeout
    inc["auto_on_timeout"] = auto_ok
    if auto_ok:
        msg = (f"Waiting {timeout}s for a human. If nobody answers, it goes ahead automatically: "
               "the rehearsal passed and the fix is low-risk.")
    else:
        inc["approval_reason"] = request["reason"]
        msg = f"Waiting up to {timeout}s for the on-call engineer ({request['reason']}). No answer means no."
    await step(inc, "awaiting_approval", "running", msg)
    try:
        approved, who = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        approved = auto_ok
        who = f"auto-approved: no answer in {timeout}s, rehearsal passed" if auto_ok else "timeout"
    finally:
        APPROVALS.pop(request["action_id"], None)
    return approved, who


async def run_verify(services):
    async def progress(s):
        STATE["services"], STATE["probe"] = s["services"], s["probe"]
        await broadcast()
    ok, last, took = await verify(monitor, services, timeout_s=30, on_progress=progress)
    if last:
        STATE["services"], STATE["probe"] = last["services"], last["probe"]
    return ok, last, took


def make_deps(inc):
    rules = safety.policy["rules"]
    return AgentDeps(
        inc=inc, step=step, broadcast=broadcast, build_context=build_context, monitor=monitor, simulator=simulator,
        safety=safety, executor=executor, known_good=known_good, memory=memory, obs=obs, ask_llm=ask_llm,
        history=lambda: STATE["history"], kill_switch=lambda: STATE["kill_switch"],
        approval_mode=lambda: STATE["approval_mode"], llm_enabled=lambda: STATE["llm_enabled"],
        run_undo=run_undo, money_follow_up=money_follow_up, security_event=security_event, verify=run_verify,
        wait_for_approval=wait_for_approval, on_shadow=lambda last: STATE["shadow"].update(last=last),
        latest_snap=lambda: LATEST.get("snap") or {}, all_changes=changes.all_changes, test_checkout=test_checkout,
        budget=TokenBudget(rules.get("investigation_token_budget", 8000)))


async def handle_incident(event, snap):
    inc = {"id": event.incident_id, "started": time.time(), "status": "open", "steps": [],
           "event": event.model_dump(mode="json"), "diagnosis": None, "proposal": None,
           "decision": None, "result": None, "recovery_seconds": None,
           "alerts": [a["name"] for a in obs.alerts()]}
    STATE["active"] = inc
    try:
        alerts = f" Alerts firing: {', '.join(inc['alerts'])}." if inc["alerts"] else ""
        await step(inc, "detected", "done",
                   f"{len(event.affected_services)} service(s) unhealthy: {', '.join(event.affected_services)}.{alerts}",
                   event.model_dump(mode="json"))
        await run_incident(make_deps(inc), event, snap)
        STATE["agent_info"]["tokens_total"] += (inc.get("agent") or {}).get("tokens", {}).get("used", 0)
        if (inc.get("agent") or {}).get("memory", {}).get("hit"):
            STATE["agent_info"]["memory_hits"] += 1
    except Exception as e:
        inc["status"] = "error"
        await step(inc, "error", "failed", f"Agent error: {e}")
    finally:
        inc["ended"] = time.time()
        await asyncio.sleep(1.5)  # let the fake customers notice the recovery before we read the impact
        inc["impact"] = read_impact_safely(inc)
        audit("incident_closed", {"incident": inc["id"], "status": inc["status"],
                                  "recovery_seconds": inc["recovery_seconds"], "impact": inc["impact"],
                                  "agent_path": [t["node"] for t in (inc.get("agent") or {}).get("trail", [])],
                                  "tokens": (inc.get("agent") or {}).get("tokens")})
        STATE["history"].insert(0, inc)
        STATE["history"] = STATE["history"][:20]
        save_history()
        STATE["active"] = None
        STATE["agent_info"]["memory"] = memory.stats()
        STATE["quiet_until"] = time.time() + (3 if inc.get("partial") else config.QUIET_SECONDS_AFTER_INCIDENT)
        await broadcast()


def count_errors(since):
    """Error/warning lines each HTTP service logged since the last tick (for the error-rate metric)."""
    out = {}
    for name, svc in config.SERVICES.items():
        if svc["health"] and (STATE["services"].get(name) or {}).get("container_status") == "running":
            out[name] = errors_since(svc["container"], since)
    return out


async def watch_loop():
    tick = 0
    last_tick = time.time()
    while True:
        try:
            snap = await asyncio.to_thread(monitor.snapshot)
            STATE["services"], STATE["probe"] = snap["services"], snap["probe"]
            errors = await asyncio.to_thread(count_errors, last_tick - 1)
            last_tick = snap["time"]
            obs.record(snap, errors)
            LATEST["snap"] = snap
            root = ((STATE["active"] or {}).get("diagnosis") or {}).get("root_cause") or {}
            STATE["observability"] = obs.public(root.get("service"))
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


@app.get("/observability")
def get_observability():
    return obs.public()


@app.get("/agent")
def get_agent():
    return {**STATE["agent_info"], "memory": memory.stats()}


@app.post("/memory/clear")
async def clear_memory():
    memory.clear()
    STATE["agent_info"]["memory"] = memory.stats()
    audit("memory_cleared", {})
    await broadcast()
    return {"ok": True}


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
