"""Keeping a long-running background loop running (blueprint §66, §117).

Both of this system's long-lived loops -- the `worker` process's workers
(`app/workers/main.py`) and the API process's live reconciliation
(`app/main.py`'s lifespan) -- were started with `asyncio.create_task` and
then never looked at again. A task that *ended*, raised or simply
returned, was invisible: the process stayed alive and the container stayed
up with the loop gone.

This lives in `app.core` rather than in either entrypoint because there
are two of them and they drifted. The worker copy was fixed first and the
API copy was missed, which is the round that produced this module.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("core.supervision")

# How long to wait before restarting a loop that ended, and the ceiling to
# back off to. Deliberately not zero: `SimulatedFeed` with no loaded
# candles returns immediately (the shipped dev default), and a zero-delay
# restart would spin that into a hot loop.
RESTART_DELAY_SECONDS = 5.0
MAX_RESTART_DELAY_SECONDS = 60.0


async def supervise(name: str, factory, stop_event: asyncio.Event | None = None) -> None:
    """Runs `factory()` forever, restarting it when it ends.

    `factory` is a callable returning a fresh coroutine, not a coroutine
    object: a coroutine can only be awaited once, so restarting needs a
    new one each time.

    `stop_event` is optional. The worker process sets one from
    SIGINT/SIGTERM; the API process has no equivalent and shuts this down
    by cancelling the task instead, which works because `CancelledError`
    is never caught here.

    Restarting in-process rather than exiting is deliberate:
    docker-compose.yml sets no `restart:` policy on any service, so
    exiting would turn a recoverable fault into a permanent outage.
    """

    def stopping() -> bool:
        return stop_event is not None and stop_event.is_set()

    delay = RESTART_DELAY_SECONDS
    while not stopping():
        try:
            await factory()
        # Deliberately `Exception`, never `BaseException`: both callers
        # shut this down by cancelling the task, and
        # `asyncio.CancelledError` derives from `BaseException` precisely
        # so a broad handler like this one cannot swallow it. Widening
        # this would turn every shutdown into a "death" to restart, and
        # the process would never exit.
        except Exception:
            logger.exception("%s died; restarting in %.0fs", name, delay)
        else:
            logger.warning("%s returned unexpectedly; restarting in %.0fs", name, delay)
        if stopping():
            break
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)
