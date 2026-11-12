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
