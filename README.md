# ShadowRun-Ops — the on-call engineer that doesn't panic

**Hackathon problem statement 4.** When a live system breaks at 3 a.m., ShadowRun-Ops notices, finds the
root cause, **practises the fix on a copy of the system first**, applies it safely with a backup, proves it
worked with a real test order, and remembers the problem so the next time costs zero AI tokens. Risky fixes
wait for the on-call engineer's approval.

---

## The three apps

| App | Port | What it is |
|---|---|---|
| **QuickCart** | 3000 | A real grocery shop (React) on 5 Docker services: storefront, cart, payment, PostgreSQL, Redis |
| **Ghost Injector** | 7070 | The "villain": breaks the shop the way real outages happen (bad deploys, bad config, crashes, freezes). Opened from a second laptop |
| **ShadowRun-Ops agent + dashboard** | 8000 / 5173 | Our LangGraph agent and the live dashboard (React, WebSocket) |

The injector never talks to the agent. The agent has to discover every problem by itself.

## How it works

```
Ghost Injector ──Docker──▶ SHOP (storefront · cart · payment · postgres · redis)
                               ▲            │
                    checks every 3 s     fixes (signed, backed up)
                               │            ▼
                         ShadowRun-Ops agent (LangGraph) ──▶ AI (Groq → Gemini → Ollama, only on an alarm)
                               │            └──▶ Shadow copy (practice shop, port 8090)
                               ▼ WebSocket
                           Dashboard
```

1. **Detect (plain code, 0 AI tokens).** Every 3 s: a synthetic customer checkout, health checks, container
   state (read-only Docker proxy), payment-vs-order consistency, log error counts. Two bad checks in a row
   raise an incident. Named alert rules and a live service map with blast radius.
2. **Triage.** A fingerprint of what was observed (failed checks + log error type + recent changes) is looked
   up in **incident memory**. Seen before → reuse the fix, **0 tokens**.
3. **Investigate.** A rule engine collects evidence (logs, change ledger, dependencies) and ranks suspects.
   A **tool-using AI investigator** then checks the evidence with read-only tools (service map, metrics, logs,
   deploy history, container, money check, test checkout) and must pick one of the ranked suspects.
   Max 5 tool calls, 8,000-token budget; if the AI fails, the rule engine decides. Logs are sanitized
   (secrets redacted, prompt-injection lines quarantined).
4. **Plan.** One of four reversible actions only: start, restart, roll back a release to the last known-good
   version, restore a config.
5. **Practise on the shadow copy.** Reproduce the same fault on an isolated copy of the shop first, then apply
   the fix there and test a checkout. The simulator can only touch `shadow-*` containers.
6. **Safety.** `policy.yaml` rules, kill switch, HMAC-signed one-time permissions (60 s). Risky actions
   (restarting the database or Redis) pause the graph (LangGraph `interrupt`) for the engineer; no answer = no.
7. **Execute + verify.** Snapshot first, then the one action; real test checkouts must pass twice. If not,
   automatic undo and the agent rethinks with that cause ruled out (max 2 retries, then a human).
8. **Learn.** Save to memory, write a postmortem (no AI). Same failure 3× in 10 min → reverse the change itself.

**Where AI is used:** only in the investigate step. Everything that watches or changes the system is
deterministic code.

## Tech stack

Python · FastAPI · Docker / Docker Compose · PostgreSQL · Redis · **LangGraph** (state graph, checkpointer,
human-in-the-loop interrupt) · Groq `openai/gpt-oss-120b` → Google Gemini → Ollama `llama3.1:8b` (local) ·
React (Vite) · WebSocket.

## Repository layout

| Folder | Contents |
|---|---|
| `agent/` | The agent: `main.py` (API, WebSocket, monitor loop), `agentic/` (LangGraph graph, observability, memory, tools, AI investigator, postmortem), `monitoring/`, `investigation/` (rule engine, sanitizer), `planner/`, `shadow/` (simulator), `safety/` (policy engine, `policy.yaml`), `execution/`, `verification/` |
| `shop/` | The QuickCart backend services, `docker-compose.yml`, the shadow stack, and `deploy.py` (deploy tool + change ledger) |
| `quickcart/` | QuickCart React storefront |
| `injector/` | Ghost Injector (`app.py` web page, `inject.py` failure scenarios) |
| `dashboard/` | ShadowRun-Ops dashboard (React) |
| `traffic/` | Fake customers (continuous orders) |
| `tools/` | `chaos_run.py` scoreboard, `check_langgraph.py` |
| `scripts/` | `start_all.ps1`, `preflight.ps1`, `demo_reset.ps1` |
| `tests/` | 101 automated tests |

## Run it (Windows, Laptop A)

Requirements: Docker Desktop, Python 3.11+, Node 18+, an API key for Groq and/or Gemini (Ollama optional).

```powershell
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env        # then add your keys and a GRANT_SECRET
cd dashboard; npm install; cd ..\quickcart; npm install; cd ..
powershell -ExecutionPolicy Bypass -File .\scripts\start_all.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\preflight.ps1     # must say READY FOR THE JUDGES
```

Open the dashboard at http://localhost:5173, QuickCart at http://localhost:3000, and the Ghost Injector at
http://localhost:7070 (or `http://<Laptop A IP>:7070` from a second laptop).

Tests: `.\venv\Scripts\python.exe -m unittest discover tests` · Real-LangGraph check: `.\venv\Scripts\python.exe tools\check_langgraph.py`

## Failure scenarios (Ghost Injector)

Bad deploy (payment 2.3 null-value bug) · slow cart release · payment gateway timeout config · wrong DB port ·
database / Redis down · database / Redis frozen (needs engineer approval) · paid-but-no-order release ·
memory leak · poisoned log (prompt injection) · rogue action (blocked by policy).

## Limits

ShadowRun-Ops fixes operational failures (bad deploys, bad config, crashes, freezes, slowness). It does not
rewrite application code; unknown failure types are escalated to a human with all the evidence. In production
the same design maps to Kubernetes, Prometheus/Grafana, CI/CD deploy history and synthetic monitoring.
