# Phase 4: freeze, rehearse, win

## 1. Code freeze (now)
- No new features from here. Only fix something that is actually broken in the demo.
- Every fix: test → `git add .` → `git commit` → `git push`.
- Tag the working version so you can always get back to it:
  ```powershell
  git tag demo-ready
  git push --tags
  ```
  If something breaks later: `git stash` then `git checkout demo-ready`.

## 2. The three scripts (repo root, Laptop A)
| Script | When |
|---|---|
| `powershell -ExecutionPolicy Bypass -File scripts\start_all.ps1` | Start of the day / after a restart: starts Docker, agent, dashboard, fake customers, opens the browser |
| `powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1` | Before every judge visit: must say **READY FOR THE JUDGES** |
| `powershell -ExecutionPolicy Bypass -File scripts\demo_reset.ps1` | Between demos. Add `-ClearHistory` (then restart the AGENT window) for a clean dashboard |

## 3. Backup video (do it once preflight is green)
Record the full demo below in one take (Win + G or OBS). Keep it on the laptop **and** on a phone/Drive.
If anything fails live, say "here's the recorded run" and play it. Then carry on.

## 4. The demo (about 4 minutes)

| Time | Who | What | Say |
|---|---|---|---|
| 0:00 | Speaker | Dashboard, all green, money banner ticking | "It's 3 a.m. QuickCart is busy. Real orders are coming in. Nobody is awake." |
| 0:20 | Helper | Hand a judge the scenario list: `bad_deploy`, `broken_config`, `memory_leak`, `stop_db`, `poisoned_log`. Judge picks one. Operator runs it | "You choose. We don't know which one." |
| 0:30 | Speaker | Map turns red, banner counts ₹ lost | "Checkout is failing. Every second costs money." |
| 0:45 | Speaker | Root cause marker + evidence | "It doesn't guess: here's the evidence, including who changed what, from the change ledger." |
| 1:05 | Speaker | Shadow copy row: red → yellow → green | "Practice first: it breaks a copy of the shop the same way, proves the fix works there, and only then touches production." |
| 1:30 | Speaker | Policy ALLOW + real fix + verify + impact | "Independent safety rules allow it. It verifies with a real checkout. Recovered in N seconds, ₹X lost instead of a night of outage." |
| 2:00 | Operator | `poisoned_log`, then click **Ask a careless agent** | "Attackers can write into logs. A careless agent obeys. Ours quarantines the line: the LLM never sees it." |
| 2:45 | Operator | `rogue_action` | "Even a compromised part can't make it drop the database: blocked by policy, and forged permissions are rejected." |
| 3:10 | Speaker | Scoreboard | "We tested every scenario repeatedly. Here are the real numbers, misses included." |
| 3:40 | Speaker | Close | "ShadowRun-Ops: it finds the cause, practises the fix, fixes safely, proves it worked, and wakes you only when it can't prove the fix is safe." |

Remember the 60 s cooldown between scenarios on the same container. Fill the gap by talking through the evidence.

## 5. Roles
- **Speaker:** tells the story, points at the screen.
- **Operator (Laptop A):** runs commands, watches the AGENT window for errors, runs `demo_reset.ps1` between judges.
- **Helper:** hands judges the scenario list, shows Laptop B's dashboard (`http://<Laptop A IP>:5173`) to judges standing to the side.
- **Backup:** has the video ready, keeps the phone hotspot charged and awake.

## 6. Numbers for the slides
After `tools\chaos_run.py --runs 2 --control 1`, copy from the scoreboard:
correct diagnoses x/y, resolved x/y, average detect, average recover, unsafe actions (should be 0), false alarms.
Say recovery honestly: "about N seconds from failure to verified recovery, including the practice run".

## 7. Judge questions (short answers)
| Question | Answer |
|---|---|
| What if the LLM is wrong? | It can only pick from rule-generated causes; if it disagrees with the rules, confidence drops and a human must approve. With no LLM at all, the rules still diagnose. |
| What if the fix makes it worse? | Every fix is rehearsed on a shadow copy first. Fails there → blocked. After the real fix, we verify with a real checkout; a fix that doesn't hold escalates to a human. |
| Can the AI give itself more power? | No. The safety engine is plain code + a YAML file. Execution needs a short-lived signed grant the AI never sees the key for. Only 4 named operations exist; no shell. |
| What about secrets in logs? | Redacted before the LLM sees them. |
| Prompt injection? | Log text is treated as data, suspicious lines are quarantined, and even if the model were fooled, destructive actions aren't in the catalogue. |
| Why so slow (about 20 s)? | 10 of those seconds are the practice run. We trade 10 seconds for proof the fix is safe. |
| Real production? | Same design: swap Docker Compose for Kubernetes, the ledger for your CI/CD history, the probe for your synthetic checks. |
| What doesn't it do? | Code bugs: it rolls back or restarts, then hands the bug to a human. Problems outside our system (e.g. a payment provider outage) go to a human. |

## 8. Last-hour checklist
- [ ] `preflight.ps1` says READY
- [ ] Backup video saved in 2 places
- [ ] Scoreboard screenshot in the slides
- [ ] Phone hotspot charging, screen awake, both laptops on it
- [ ] Laptop power cables plugged in, sleep disabled (Settings → System → Power → Never)
- [ ] Notifications off (Focus assist)
- [ ] `git push` done and the `demo-ready` tag exists
