"""Verification: proves the live system recovered. Simulation success alone is never proof."""
import asyncio
import time

NEEDED_GOOD_POLLS = 2


async def verify(monitor, services, timeout_s=60, interval_s=3, on_progress=None):
    started, good = time.time(), 0
    last = None
    while time.time() - started < timeout_s:
        last = await asyncio.to_thread(monitor.snapshot)
        if monitor.is_healthy(last, services):
            good += 1
            if good >= NEEDED_GOOD_POLLS:
                return True, last, round(time.time() - started, 1)
        else:
            good = 0
        if on_progress:
            await on_progress(last)
        await asyncio.sleep(interval_s)
    return False, last, round(time.time() - started, 1)


def judge_partial(last, target_service):
    """The whole shop isn't healthy yet. Was the agent's fix itself OK?

    "partial": the fixed service is healthy now, or it can't be judged yet because something it depends
               on is still broken (another problem). Keep the fix and go after the next cause.
    "failed":  the fixed service is still unhealthy although everything it depends on is fine, so the fix
               really didn't work. Reverse it.
    """
    from agent import config
    services = (last or {}).get("services", {})
    target = services.get(target_service)
    if target is None:
        return "failed"
    if target.get("health") == "ok":
        return "partial"
    deps_broken = [d for d in config.DEPENDS_ON.get(target_service, [])
                   if services.get(d, {}).get("health") != "ok"]
    return "partial" if deps_broken else "failed"
