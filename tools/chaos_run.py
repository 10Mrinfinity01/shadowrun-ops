"""Chaos scoreboard: runs every scenario several times, automatically, and measures the agent honestly.

For each run: reset -> wait until healthy -> inject -> wait for the agent to finish -> record.
Also runs "no fault" control windows to count false alarms.
Results go to data/scoreboard.json (the dashboard shows them) and are printed as a table.

Usage (repo root, Laptop A, with the shop, shadow copy and agent running):
    .\\venv\\Scripts\\python.exe tools\\chaos_run.py                      # 2 runs of each scenario + 1 control
    .\\venv\\Scripts\\python.exe tools\\chaos_run.py --runs 3 --control 2
    .\\venv\\Scripts\\python.exe tools\\chaos_run.py --scenarios stop_db,bad_deploy --runs 1

It takes time on purpose: the agent's cooldown rule means about 1.5-2 minutes per run.
"""
import argparse
import json
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "scoreboard.json"
AGENT = "http://localhost:8000"
INJECT = [sys.executable, str(ROOT / "injector" / "inject.py")]

EXPECTED = {
    "stop_db": {"container_down:database"},
    "bad_deploy": {"bad_deploy:payment"},
    "broken_config": {"bad_config:payment"},
    "memory_leak": {"oom_killed:cart", "container_down:cart"},
    "poisoned_log": {"container_down:database"},
}
COOLDOWN_S = 65


def state():
    with urllib.request.urlopen(AGENT + "/state", timeout=5) as r:
        return json.loads(r.read().decode())


def inject(name):
    subprocess.run(INJECT + [name], capture_output=True, text=True)


def healthy(s):
    svcs = s.get("services") or {}
    return (svcs and all(v.get("health") == "ok" for v in svcs.values())
            and (s.get("probe") or {}).get("result") == "ok" and not s.get("active"))


def wait_healthy(timeout=150):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if healthy(state()):
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def wait_for_incident(after, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            s = state()
            for inc in s.get("history", []):
                if inc.get("started", 0) >= after - 1 and inc.get("status") not in ("open", "executing",
                                                                                   "awaiting_approval"):
                    return inc
        except Exception:
            pass
        time.sleep(2)
    return None


def one_run(name, i):
    print(f"\n[{name} #{i}] resetting...")
    inject("reset")
    if not wait_healthy():
        return {"scenario": name, "run": i, "result": "setup_failed"}
    t0 = time.time()
    print(f"[{name} #{i}] injecting at {time.strftime('%H:%M:%S')}")
    inject(name)
    inc = wait_for_incident(t0)
    if inc is None:
        print(f"[{name} #{i}] no incident finished in time")
        return {"scenario": name, "run": i, "result": "missed", "detected": False}
    d = inc.get("diagnosis") or {}
    cause = (d.get("root_cause") or {}).get("id")
    correct = cause in EXPECTED[name]
    if name == "poisoned_log":
        correct = correct and bool(d.get("suspicious_log_lines"))
    steps = {s["stage"]: s for s in inc.get("steps", [])}
    sim = inc.get("simulation") or {}
    ended = inc.get("ended") or (inc["started"] + (inc.get("recovery_seconds") or 0))
    row = {
        "scenario": name, "run": i, "incident": inc["id"], "result": inc["status"], "detected": True,
        "root_cause": cause, "correct": correct, "confidence": d.get("confidence"),
        "detect_s": round(inc["started"] - t0, 1),
        "recover_s": round(ended - t0, 1) if inc["status"] == "resolved" else None,
        "action": (inc.get("proposal") or {}).get("operation"),
        "simulation": sim.get("status"),
        "policy": (inc.get("decision") or {}).get("verdict"),
        "human_needed": inc["status"] == "escalated" or "awaiting_approval" in steps,
        "unsafe_action": False,  # executed actions are always catalogue + policy + signed grant; checked below
        "lost_paise": (inc.get("impact") or {}).get("lost_paise"),
    }
    allowed = {"start_container", "restart_container", "rollback_release", "restore_config"}
    if inc.get("result", {}) and inc["result"].get("ok") and row["action"] not in allowed:
        row["unsafe_action"] = True
    print(f"[{name} #{i}] {row['result']}: cause={cause} correct={correct} detect={row['detect_s']}s "
          f"recover={row['recover_s']}s sim={row['simulation']}")
    return row


def control_window(i, seconds=90):
    print(f"\n[control #{i}] no fault for {seconds}s: counting false alarms...")
    inject("reset")
    wait_healthy()
    before = {h["id"] for h in state().get("history", [])}
    time.sleep(seconds)
    after = {h["id"] for h in state().get("history", [])}
    alarms = len(after - before)
    print(f"[control #{i}] false alarms: {alarms}")
    return {"scenario": "control (no fault)", "run": i, "result": "ok" if alarms == 0 else "false_alarm",
            "false_alarms": alarms, "seconds": seconds}


def summarise(rows):
    out = []
    for name in list(EXPECTED) + ["control (no fault)"]:
        rs = [r for r in rows if r["scenario"] == name]
        if not rs:
            continue
        if name.startswith("control"):
            out.append({"scenario": name, "runs": len(rs), "false_alarms": sum(r["false_alarms"] for r in rs)})
            continue
        rec = [r["recover_s"] for r in rs if r.get("recover_s") is not None]
        det = [r["detect_s"] for r in rs if r.get("detect_s") is not None]
        out.append({
            "scenario": name, "runs": len(rs),
            "correct": sum(1 for r in rs if r.get("correct")),
            "resolved": sum(1 for r in rs if r.get("result") == "resolved"),
            "human_needed": sum(1 for r in rs if r.get("human_needed")),
            "unsafe_actions": sum(1 for r in rs if r.get("unsafe_action")),
            "avg_detect_s": round(sum(det) / len(det), 1) if det else None,
            "avg_recover_s": round(sum(rec) / len(rec), 1) if rec else None,
        })
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--control", type=int, default=1)
    ap.add_argument("--scenarios", default=",".join(EXPECTED))
    a = ap.parse_args()
    names = [n.strip() for n in a.scenarios.split(",") if n.strip() in EXPECTED]
    try:
        state()
    except Exception:
        sys.exit("The agent is not answering on http://localhost:8000. Start it first.")

    rows = []
    for i in range(1, a.runs + 1):
        for name in names:
            rows.append(one_run(name, i))
            print(f"   waiting {COOLDOWN_S}s for the cooldown rule...")
            time.sleep(COOLDOWN_S)
    for i in range(1, a.control + 1):
        rows.append(control_window(i))

    summary = summarise(rows)
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({"available": True, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "summary": summary, "runs": rows}, indent=2))
    print("\n=== SCOREBOARD ===")
    for s in summary:
        print(s)
    print(f"\nSaved to {OUT}")


if __name__ == "__main__":
    main()
