"""Shadow simulation: practise the fix on a copy of the shop before touching the real one.

Rule: REPRODUCE FIRST, THEN FIX.
  1. Baseline   the shadow copy must be healthy (checkout works) before we start.
  2. Reproduce  put the same fault into the copy and confirm checkout FAILS there too.
                If the failure cannot be reproduced, a green result would prove nothing,
                so the result is "not_reproducible" and a human must approve.
  3. Apply      run the proposed fix against the copy.
  4. Verify     checkout must work in the copy (2 good checks in a row).
  5. Restore    put the copy back to its known-good state for next time.

The simulator can only ever touch containers whose names start with "shadow-".
"""
import time

from agent import changes

TWIN = {"demo-db": "shadow-db", "shop-cart": "shadow-cart", "shop-redis": "shadow-redis",
        "shop-payment": "shadow-payment", "shop-storefront": "shadow-storefront"}
SHADOW_CONTAINERS = list(TWIN.values())
SHADOW_PROBE = "http://localhost:8090/api/probe"


class ShadowRuntime:
    """Same file format as the live runtime, but under data/shadow-runtime."""

    @staticmethod
    def _dir():
        return changes.DATA / "shadow-runtime"

    def read(self, service, kind):
        import json
        try:
            return {**changes.DEFAULTS[kind], **json.loads((self._dir() / f"{service}.{kind}.json").read_text())}
        except FileNotFoundError:
            return dict(changes.DEFAULTS[kind])

    def write(self, service, kind, value):
        import json
        self._dir().mkdir(parents=True, exist_ok=True)
        (self._dir() / f"{service}.{kind}.json").write_text(json.dumps(value, indent=2))


SHADOW_CONSISTENCY = "http://localhost:9082/internal/consistency"


class ShadowProbe:
    """Same checks as live monitoring, on the copy: checkout works, is fast enough, and money matches orders."""

    def __init__(self):
        self.started = time.time()
        self.last = None

    def __call__(self):
        import httpx
        from agent import config
        t0 = time.time()
        try:
            r = httpx.post(SHADOW_PROBE, timeout=4)
            ms = int((time.time() - t0) * 1000)
            if r.status_code >= 400:
                return False, f"HTTP {r.status_code}"
            if ms > config.PROBE_SLOW_MS:
                return False, f"too slow: {ms} ms"
        except Exception as e:
            return False, f"unreachable: {str(e)[:80]}"
        try:
            now = httpx.get(SHADOW_CONSISTENCY, params={"since": self.started}, timeout=3).json()
            prev, self.last = self.last or now, now
            if (now["payments_without_order"] > prev["payments_without_order"]
                    or now["status_mismatch"] > prev["status_mismatch"]):
                return False, "HTTP 200 but payment/order consistency check failed"
        except Exception:
            pass
        return True, f"HTTP 200 in {ms} ms"


def default_probe():
    return ShadowProbe()()


class ShadowSimulator:
    def __init__(self, docker_url=None, probe=None, runtime=None, sleep=time.sleep,
                 reproduce_timeout_s=12, verify_timeout_s=25, interval_s=1.0):
        self.docker_url = docker_url
        self.probe = probe or ShadowProbe()
        self.runtime = runtime or ShadowRuntime()
        self.sleep = sleep
        self.reproduce_timeout_s = reproduce_timeout_s
        self.verify_timeout_s = verify_timeout_s
        self.interval_s = interval_s
        self._docker = None

    # ---- docker, restricted to shadow containers ----------------------------------------------
    def _client(self):
        if self._docker is None:
            import docker
            from agent import config
            self._docker = docker.DockerClient(base_url=self.docker_url or config.DOCKER_EXEC_URL, timeout=20)
        return self._docker

    def _container(self, name):
        if name not in SHADOW_CONTAINERS:  # hard wall: never a live container
            raise PermissionError(f"shadow simulator may not touch {name}")
        return self._client().containers.get(name)

    def _start(self, name):
        c = self._container(name)
        if c.status != "running":
            c.start()

    def _stop(self, name):
        self._container(name).stop(timeout=3)

    def _restart(self, name):
        self._container(name).restart(timeout=3)

    # ---- helpers ----------------------------------------------------------------------------
    def _wait_for(self, want_ok, timeout_s, needed=1):
        """Poll the shadow checkout until it is ok (want_ok=True) or failing (False) `needed` times in a row."""
        started, streak, detail = time.time(), 0, ""
        while time.time() - started < timeout_s:
            ok, detail = self.probe()
            streak = streak + 1 if ok == want_ok else 0
            if streak >= needed:
                return True, detail, round(time.time() - started, 1)
            self.sleep(self.interval_s)
        return False, detail, round(time.time() - started, 1)

    def _restore(self, log):
        """Make the copy match the live shop's known-good releases/configs, with every container running."""
        good = changes.KnownGood()
        changed = []
        for svc in changes.MANAGED:
            for kind in ("release", "config"):
                target = good.get(svc, kind)
                if self.runtime.read(svc, kind) != target:
                    self.runtime.write(svc, kind, target)
                    changed.append(svc)
        for name in SHADOW_CONTAINERS:
            self._start(name)
        for svc in set(changed):
            self._restart(f"shadow-{svc}")
        log("restore", "done", "shadow copy put back to its known-good state")

    # ---- the simulation ---------------------------------------------------------------------
    def simulate(self, proposal, on_step=None):
        started = time.time()
        steps = []

        def log(stage, status, detail):
            step = {"stage": stage, "status": status, "detail": detail}
            if status != "running":  # "running" steps are progress updates for the live view only
                steps.append(step)
            if on_step:
                try:
                    on_step(step)  # lets the dashboard show the rehearsal live
                except Exception:
                    pass

        def result(status, reproduced=False, fixed=False):
            return {"status": status, "reproduced": reproduced, "fixed": fixed, "steps": steps,
                    "duration_s": round(time.time() - started, 1)}

        twin = TWIN.get(proposal.target)
        op, p = proposal.operation, proposal.parameters
        try:
            if twin is None:
                log("reproduce", "skipped", f"no shadow twin for {proposal.target}")
                return result("not_reproducible")
            if op == "restart_container":
                log("reproduce", "skipped", "a generic 'unhealthy service' cannot be recreated faithfully in the copy")
                return result("not_reproducible")

            # 1. baseline
            ok, detail, took = self._wait_for(True, 3)
            if not ok:
                self._restore(log)
                ok, detail, took = self._wait_for(True, 20)
                if not ok:
                    log("baseline", "failed", f"shadow copy is not healthy ({detail}); cannot rehearse")
                    return result("error")
            log("baseline", "ok", "shadow checkout works before the test")

            # 2. reproduce the fault in the copy
            log("reproduce", "running", f"putting the same fault into {twin}")
            if op == "start_container":
                self._stop(twin)
                what = f"stopped {twin}"
            elif op in ("rollback_release", "restore_config"):
                self.runtime.write(p["service"], p["kind"], p["from"])
                self._restart(twin)
                what = (f"deployed the same {p['kind']} to {twin}: "
                        + (f"release {p['from'].get('version')}" if p["kind"] == "release" else str(p["from"])))
            else:
                log("reproduce", "skipped", f"no way to rehearse {op}")
                return result("not_reproducible")
            failed, detail, took = self._wait_for(False, self.reproduce_timeout_s, needed=2)
            if not failed:
                log("reproduce", "not reproduced", f"{what}, but shadow checkout still works ({detail})")
                self._restore(log)
                return result("not_reproducible")
            log("reproduce", "red", f"{what} -> shadow checkout FAILS too ({detail}), fault reproduced in {took}s")

            # 3. apply the proposed fix to the copy
            log("apply", "running", f"applying {op} to {twin}")
            if op == "start_container":
                self._start(twin)
                applied = f"started {twin}"
            else:
                self.runtime.write(p["service"], p["kind"], p["to"])
                self._restart(twin)
                applied = (f"{op} on {twin}: " + (f"release {p['to'].get('version')}" if p["kind"] == "release"
                                                  else f"config {p['to']}"))
            log("apply", "done", applied)

            # 4. verify in the copy
            fixed, detail, took = self._wait_for(True, self.verify_timeout_s, needed=2)
            if fixed:
                log("verify", "green", f"shadow checkout works again ({detail}) after {took}s")
            else:
                log("verify", "failed", f"shadow checkout still failing after {took}s ({detail}): the fix does not work")
            self._restore(log)
            return result("passed" if fixed else "failed", reproduced=True, fixed=fixed)
        except Exception as e:
            log("error", "failed", f"simulation error: {str(e)[:160]}")
            self._docker = None
            try:
                self._restore(log)
            except Exception:
                pass
            return result("error")
