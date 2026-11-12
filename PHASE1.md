# Phase 1: the walking skeleton

Goal: **database down → detected → diagnosed → fixed → verified → shown on the dashboard.**

## What's in this drop

| Path | What it does | Owner |
|---|---|---|
| `shop/` | QuickCart: storefront, cart, payment, Postgres + two Docker socket proxies | Laptop A |
| `injector/inject.py` | Judge Injector (command line for now): `stop_db`, `reset`, `status` | Laptop A |
| `agent/monitoring/` | Health checks, container states, customer checkout probe, incident detection | Laptop A |
| `agent/execution/` | The only component that changes anything; checks the signed grant first | Laptop A |
| `agent/verification/` | Proves the live shop recovered (2 healthy polls in a row + a real checkout) | Laptop A |
| `agent/contracts.py` + `contracts/` | The 4 JSON contracts between modules | Laptop B |
| `agent/llm.py` | Groq → Gemini → Ollama, automatic fallback | Laptop B |
| `agent/sanitizer/` | Removes secrets from logs, quarantines instructions aimed at the AI | Laptop B |
| `agent/investigation/` | Rules rank causes; the LLM reviews and explains, but can only pick from the rule candidates | Laptop B |
| `agent/planner/` | Picks one action from a fixed catalogue | Laptop B |
| `agent/safety/` | `policy.yaml` + engine: ALLOW / APPROVAL_REQUIRED / BLOCK, signed short-lived grants | Laptop B |
| `agent/main.py` | Runs the pipeline, serves `/state`, `/ws`, approve/deny, kill switch | both |
| `dashboard/src/` | Live dashboard | Laptop B |
| `tests/test_phase1.py` | 20 logic tests, no Docker needed | both |

## Run it (Laptop A, PowerShell, repo root)

1. Start Docker Desktop and wait for "Engine running".
2. Start the shop (first time takes a few minutes: it builds the images):
   ```powershell
   cd shop
   docker compose up -d --build
   cd ..
   ```
3. Open http://localhost:8080 , add an item and click **Pay now**. You should get "Order placed".
4. Run the logic tests:
   ```powershell
   .\venv\Scripts\python.exe -m unittest tests.test_phase1 -v
   ```
5. Start the agent:
   ```powershell
   .\venv\Scripts\python.exe -m uvicorn agent.main:app --host 0.0.0.0 --port 8000
   ```
6. In a second PowerShell window, start the dashboard:
   ```powershell
   cd dashboard
   npm run dev -- --host
   ```
   Open http://localhost:5173 . All five cards should turn green within a few seconds.
7. In a third window, break it:
   ```powershell
   .\venv\Scripts\python.exe injector\inject.py stop_db
   ```
   Watch the dashboard: cards go red → incident detected → root cause `database` → `start_container demo-db` → ALLOW → verified → resolved, with the time to recovery.

Reset any time with `.\venv\Scripts\python.exe injector\inject.py reset`.

## Laptop B

```powershell
git pull
cd dashboard
copy .env.example .env.local      # then put Laptop A's real IP in it
npm run dev
```
Laptop A must be running the agent (step 5) and the firewall must allow port 8000.

## Things to try once it works

- **Human approval:** in `agent/safety/policy.yaml` set `min_confidence_for_auto: 0.99`, restart the agent, run `stop_db`. The dashboard shows Approve / Deny buttons. Wait 120 s without clicking and nothing is changed.
- **Kill switch:** click it on the dashboard, run `stop_db`. The agent diagnoses but is blocked from acting.
- **Cooldown:** run `stop_db` twice within 60 s. The second fix is blocked by the cooldown rule.
- **No LLM:** take the internet away. Groq and Gemini fail, Ollama answers. Even with no LLM at all, the rules still diagnose correctly.

Everything the agent does is recorded in `data/audit.jsonl`.

## Troubleshooting

| Problem | Fix |
|---|---|
| Dashboard says "agent offline" | Is step 5 running? On Laptop B, is the IP in `.env.local` correct? |
| Cards say `unknown (...)` for containers | The socket proxies aren't running: `cd shop; docker compose ps` |
| `docker compose up` fails while building | It needs internet the first time (it installs Python packages inside the image) |
| Policy says "GRANT_SECRET missing" | Fill `GRANT_SECRET=` in `.env` |
| Port 8080/8081/8082 already in use | Close whatever uses it, or change the left-hand port in `shop/docker-compose.yml` and `agent/config.py` |

## Known Phase 1 limits (said honestly)

- Shadow simulation is a placeholder step; it arrives in Phase 2.
- The write proxy allows any container POST at the proxy level; the Execution engine's allowlist is what limits it to start/restart of shop containers.
- Approval happens on the dashboard only; Telegram comes in Phase 2.
