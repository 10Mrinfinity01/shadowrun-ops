"""Monitoring: watches the shop independently. It is never told what the injector did."""
import time
import uuid
from datetime import datetime, timezone

from agent import config
from agent.contracts import IncidentEvent, Signal


class Monitor:
    def __init__(self):
        self._docker = None
        self.bad_streak = 0

    # ---- low-level readers -------------------------------------------------------------------
    def _client(self):
        if self._docker is None:
            import docker
            self._docker = docker.DockerClient(base_url=config.DOCKER_READ_URL, timeout=5)
        return self._docker

    def container_states(self):
        """container -> {"status", "oom_killed", "exit_code"}"""
        try:
            found = {c.name: c for c in self._client().containers.list(all=True)}
        except Exception as e:
            self._docker = None
            return {svc["container"]: {"status": f"unknown ({str(e)[:60]})", "oom_killed": False, "exit_code": None}
                    for svc in config.SERVICES.values()}
        out = {}
        for svc in config.SERVICES.values():
            c = found.get(svc["container"])
            if c is None:
                out[svc["container"]] = {"status": "missing", "oom_killed": False, "exit_code": None}
                continue
            info = {"status": c.status, "oom_killed": False, "exit_code": None}
            if c.status != "running":
                try:  # the list call has no exit details; inspect only the stopped ones
                    state = self._client().containers.get(c.name).attrs.get("State", {})
                    info["oom_killed"] = bool(state.get("OOMKilled"))
                    info["exit_code"] = state.get("ExitCode")
                except Exception:
                    pass
            out[svc["container"]] = info
        return out

    def logs(self, container, lines=30, since=None):
        try:
            kw = {"tail": lines}
            if since:
                kw["since"] = int(since)
            raw = self._client().containers.get(container).logs(**kw)
            return raw.decode("utf-8", "replace").splitlines()
        except Exception as e:
            return [f"(could not read logs: {str(e)[:80]})"]

    @staticmethod
    def _http(method, url):
        import httpx
        try:
            r = httpx.request(method, url, timeout=3)
            return r.status_code < 400, f"HTTP {r.status_code}: {r.text[:160]}"
        except Exception as e:
            return False, f"unreachable: {str(e)[:120]}"

    # ---- one look at the whole system --------------------------------------------------------
    def snapshot(self):
        states = self.container_states()
        services = {}
        for name, svc in config.SERVICES.items():
            cs = states.get(svc["container"], {"status": "missing", "oom_killed": False, "exit_code": None})
            status = cs["status"]
            if svc["health"] and status == "running":
                ok, detail = self._http("GET", svc["health"])
                health = "ok" if ok else "failed"
            elif svc["health"]:
                health, detail = "failed", f"container is {status}"
            else:  # the database has no HTTP endpoint: running means healthy at this level
                health = "ok" if status == "running" else "failed"
                detail = f"container is {status}"
            if cs["oom_killed"]:
                detail = f"container is {status}: OOM-killed (out of memory, exit code {cs['exit_code']})"
            elif status != "running" and cs["exit_code"] is not None:
                detail = f"container is {status} (exit code {cs['exit_code']})"
            services[name] = {"container": svc["container"], "container_status": status,
                              "oom_killed": cs["oom_killed"], "exit_code": cs["exit_code"],
                              "health": health, "detail": detail}
        probe_ok, probe_detail = self._http("POST", config.CUSTOMER_PROBE_URL)
        return {"time": time.time(), "services": services,
                "probe": {"result": "ok" if probe_ok else "failed", "detail": probe_detail}}

    @staticmethod
    def bad_services(snap):
        return [n for n, s in snap["services"].items() if s["health"] != "ok"]

    @staticmethod
    def is_healthy(snap, services=None):
        names = services or list(snap["services"])
        return all(snap["services"][n]["health"] == "ok" for n in names) and snap["probe"]["result"] == "ok"

    @staticmethod
    def signals(snap):
        out = []
        for name, s in snap["services"].items():
            out.append(Signal(type="container_state", target=name,
                              result="ok" if s["container_status"] == "running" else "failed",
                              detail=s["container_status"]))
            out.append(Signal(type="health_check", target=name,
                              result="ok" if s["health"] == "ok" else "failed", detail=s["detail"]))
        out.append(Signal(type="customer_probe", target="checkout",
                          result=snap["probe"]["result"], detail=snap["probe"]["detail"]))
        return out

    def check_for_incident(self, snap, in_deploy_window=False):
        """Returns an IncidentEvent after BAD_POLLS_BEFORE_INCIDENT bad polls in a row, else None.
        Stays quiet during a deploy window (a deploy restarts containers on purpose)."""
        if in_deploy_window:
            self.bad_streak = 0
            return None
        bad = self.bad_services(snap)
        if not bad and snap["probe"]["result"] == "ok":
            self.bad_streak = 0
            return None
        self.bad_streak += 1
        if self.bad_streak < config.BAD_POLLS_BEFORE_INCIDENT:
            return None
        self.bad_streak = 0
        severity = "critical" if snap["probe"]["result"] == "failed" else "high"
        return IncidentEvent(
            incident_id="SR-" + uuid.uuid4().hex[:6].upper(),
            detected_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            severity=severity,
            affected_services=bad or ["checkout"],
            signals=[s for s in self.signals(snap) if s.result == "failed"],
        )
