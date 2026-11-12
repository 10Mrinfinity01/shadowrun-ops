# Phase 3: the wow features + real numbers

| Feature | What judges see |
|---|---|
| **Live service map** | The shop as connected boxes. When something breaks, red spreads along the dependencies and the real cause gets a **◀ ROOT CAUSE** marker. Underneath, the **shadow copy** lights up live during the practice run: twin turns red (fault reproduced) → yellow (applying fix) → green (fix works). |
| **Fake customers + money lost** | `traffic\traffic.py` places real orders nonstop. During an outage a red banner counts **failed checkouts and ₹ lost**, live. Each incident records its customer impact (also in History). |
| **Careless agent vs ShadowRun-Ops** | After `poisoned_log`, click **Ask a careless agent**. The same poisoned logs go raw into an LLM with no sanitizer and no policy (Ollama first, then the others). Its command is shown next to what ShadowRun did. **It is never executed.** The verdict is honest: if the model resists this time, the card says so. |
| **Chaos scoreboard** | `tools\chaos_run.py` runs every scenario several times on its own, plus "no fault" control windows, and measures correct diagnoses, resolved, humans needed, unsafe actions, average detect and recover times, and false alarms. The dashboard shows the table. |

Also: the shadow storefront is on port **8090** (the fix you made), and the LLM timeout is 8 s.

## Install (Laptop A)

1. Stop the agent and the dashboard (Ctrl + C).
2. Extract, replacing files:
   ```powershell
   Expand-Archive -Path shadowrun-ops-phase3.zip -DestinationPath . -Force
   del shadowrun-ops-phase3.zip
   ```
3. Tests: `.\venv\Scripts\python.exe -m unittest tests.test_phase1 tests.test_phase2 tests.test_phase2b tests.test_phase3` should end in `Ran 50 tests ... OK`.
4. Push: `git add .`, `git commit -m "Phase 3"`, `git push`. Laptop B: `git pull`.

No Docker rebuild is needed this time (the shop didn't change).

## Run everything (4 windows on Laptop A)

| Window | Command |
|---|---|
| 1. Agent | `.\venv\Scripts\python.exe -m uvicorn agent.main:app --host 0.0.0.0 --port 8000` |
| 2. Dashboard | `cd dashboard` then `npm run dev -- --host` |
| 3. Fake customers | `.\venv\Scripts\python.exe traffic\traffic.py` |
| 4. Injector | `.\venv\Scripts\python.exe injector\inject.py <scenario>` |

(The shop and the shadow copy keep running in Docker. Check with `docker ps` if unsure.)

## Show it

1. `inject.py bad_deploy`: watch the map turn red, the money counter climb, the shadow copy go red → green, then everything recover. The incident card shows the customer impact.
2. `inject.py poisoned_log`, wait for "resolved", then click **Ask a careless agent**.
3. `inject.py rogue_action` for the security events.

## Get your real numbers (do this once, about 20 minutes)

With windows 1–3 running:
```powershell
.\venv\Scripts\python.exe tools\chaos_run.py --runs 2 --control 1
```
Leave it alone: it resets, injects, waits for the agent, waits out the 60 s cooldown, and repeats.
At the end click **Refresh** on the dashboard's scoreboard. Put these numbers in your slides,
including any misses: judges trust honest numbers.

Quicker check: `tools\chaos_run.py --scenarios stop_db,bad_deploy --runs 1 --control 0` (about 5 minutes).

## Troubleshooting

| Problem | Fix |
|---|---|
| "Fake customers are not running" | Start window 3. The banner needs fresh stats (less than 6 s old) |
| Careless agent says "No LLM answered" | Is Ollama running? (`ollama list`) Groq/Gemini are tried after it |
| Careless agent didn't follow the injection | That's the real model's answer, shown honestly. Click again (models vary), or say: "we don't rely on the model resisting" |
| Scoreboard shows `setup_failed` | The shop didn't get healthy after reset. Check `inject.py status`, then rerun |
| Laptop is slow | 8 containers + Ollama + traffic. Use `traffic.py --rate 1` and close other apps |
