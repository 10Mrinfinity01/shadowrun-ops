# Phase 2b: shadow simulation ("Practice first. Then real.")

Before any fix touches the real shop, the agent rehearses it on a **shadow copy**:

1. **Baseline:** the copy's checkout must work.
2. **Reproduce:** put the same fault into the copy (stop shadow-db / deploy the same bad 2.3 / the same bad config) and confirm checkout **fails** there too.
3. **Apply:** run the proposed fix against the copy.
4. **Verify:** the copy's checkout must work again (2 good checks in a row).
5. **Restore:** put the copy back to the known-good state.

| Rehearsal result | What Safety does |
|---|---|
| passed (reproduced + fixed) | normal rules: ALLOW if confident and reversible |
| failed (the fix didn't work in the copy) | **BLOCK**: never tried on the real shop |
| not reproducible (the copy didn't break) | **APPROVAL_REQUIRED**: a green result would prove nothing |
| error / not run | **APPROVAL_REQUIRED** |

The simulator has a hard wall: it can only touch containers named `shadow-*`.

## Install (Laptop A)

1. Stop the agent (Ctrl + C) and the dashboard.
2. Extract the zip into `shadowrun-ops`, replacing files:
   ```powershell
   Expand-Archive -Path shadowrun-ops-phase2b.zip -DestinationPath . -Force
   del shadowrun-ops-phase2b.zip
   mkdir data\shadow-runtime -Force
   ```
3. Start the shadow copy (first time builds; the real shop keeps running):
   ```powershell
   cd shop
   docker compose -f docker-compose.shadow.yml up -d --build
   cd ..
   ```
4. Check the copy works: open http://localhost:9080 , add an item and click **Pay now**.
5. Tests: `.\venv\Scripts\python.exe -m unittest tests.test_phase1 tests.test_phase2 tests.test_phase2b` should end in `Ran 44 tests ... OK`.
6. Start the agent and the dashboard as before. The header should show **"shadow copy ready"**.
7. Push: `git add .`, `git commit -m "Phase 2b: shadow simulation"`, `git push`. Laptop B: `git pull`.

## Try it

Run any scenario, e.g. `.\venv\Scripts\python.exe injector\inject.py bad_deploy`.
The incident card now has a **Practice run** row: baseline ok → reproduce (red) → apply → verify (green).
Recovery takes a little longer (about 15–25 s) because the fix is rehearsed first. That's the point: say so in the demo.

To show the judges the **Approve / Deny** buttons: in `agent\safety\policy.yaml` set
`min_confidence_for_auto: 0.99`, restart the agent, run a scenario, and let a judge click Approve.
Set it back to `0.7` afterwards.

## Troubleshooting

| Problem | Fix |
|---|---|
| Header says "shadow copy offline" | `cd shop; docker compose -f docker-compose.shadow.yml ps`, then start it with `up -d` |
| Practice run says "error: shadow copy is not healthy" | Open http://localhost:9080 and check checkout works there |
| Every practice run says "not reproduced" | The shadow copy may already be broken. Restart it: `docker compose -f docker-compose.shadow.yml restart` |
| Port 9080/9081/9082 in use | Change the left-hand ports in `docker-compose.shadow.yml` and `SHADOW_PROBE` in `agent\shadow\simulator.py` |
| Laptop slows down | The shop + its copy = 8 containers plus Ollama. Close other apps; you can stop Ollama if Groq works |

## Say this in the demo

"Other agents fix first and hope. Ours rehearses the fix on a copy of the shop first: it breaks the copy the same way, proves the fix works there, and only then touches production. If the rehearsal fails, the fix is blocked."
