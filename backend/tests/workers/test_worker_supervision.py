"""One transient Redis error used to kill a worker for good, silently.

`heartbeat` is a bare `redis.set` with no error handling
(app/core/redis.py), and both worker loops called it *outside* the `try`
that guards each pass. Measured before this: a single `ConnectionError`
raised straight out of `while True`, ending the task after one pass.

Nothing noticed. `app/workers/main.py` created each worker with
`asyncio.create_task` and then sat in `await stop_event.wait()`, which
only SIGINT/SIGTERM ever sets, so the process stayed alive and the
container stayed up with the worker gone -- and at shutdown
`gather(..., return_exceptions=True)` collected the exception and threw it
away, so the cause was never even logged. For unattended autonomous
trading (§54) and worker health (§117) that is the failure you must not
have quietly.
"""

import asyncio

import pytest

import app.workers.auto_trade_worker as auto_trade_module
import app.core.supervision as supervision
import app.workers.scanner_worker as scanner_module
from app.workers.auto_trade_worker import AutoTradeSupervisor
from app.workers.scanner_worker import ScannerWorker


async def _run_briefly(coro_fn, seconds: float = 0.2) -> bool:
    """True if the loop was still running when we stopped it ourselves."""
    try:
        await asyncio.wait_for(coro_fn(), timeout=seconds)
    except asyncio.TimeoutError:
        return True
    return False


# --- the heartbeat must not be the call that kills the loop ---------------


@pytest.mark.parametrize(
    "module, worker_cls",
    [(scanner_module, ScannerWorker), (auto_trade_module, AutoTradeSupervisor)],
    ids=["scanner", "autotrade"],
)
async def test_a_transient_redis_error_in_the_heartbeat_does_not_end_the_loop(
    monkeypatch, module, worker_cls
):
    passes = {"n": 0}

    async def run_once():
        passes["n"] += 1

    async def failing_heartbeat(worker_name: str):
        raise ConnectionError("Redis went away for a moment")

    monkeypatch.setattr(module, "heartbeat", failing_heartbeat)
    worker = worker_cls(timeframe="15m", interval_seconds=0.0)
    worker.run_once = run_once

    assert await _run_briefly(worker.run) is True
    # And it kept working, rather than merely not crashing.
    assert passes["n"] > 1


@pytest.mark.parametrize(
    "module, worker_cls",
    [(scanner_module, ScannerWorker), (auto_trade_module, AutoTradeSupervisor)],
    ids=["scanner", "autotrade"],
)
async def test_a_failing_pass_still_heartbeats_and_keeps_looping(monkeypatch, module, worker_cls):
    # Control: the existing semantics must survive the fix. A pass that
    # raises is already tolerated, and the heartbeat still fires -- it
    # means "this loop is alive", which it is.
    beats = {"n": 0}

    async def run_once():
        raise RuntimeError("this pass failed")

    async def counting_heartbeat(worker_name: str):
        beats["n"] += 1

    monkeypatch.setattr(module, "heartbeat", counting_heartbeat)
    worker = worker_cls(timeframe="15m", interval_seconds=0.0)
    worker.run_once = run_once

    assert await _run_briefly(worker.run) is True
    assert beats["n"] > 1


# --- a dead worker must be noticed and brought back ----------------------


async def test_supervise_restarts_a_worker_that_raises(monkeypatch):
    monkeypatch.setattr(supervision, "RESTART_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(supervision, "MAX_RESTART_DELAY_SECONDS", 0.01)
    starts = {"n": 0}
    stop_event = asyncio.Event()

    async def dies():
        starts["n"] += 1
        raise ConnectionError("Redis went away for a moment")

    task = asyncio.create_task(supervision.supervise("autotrade", dies, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # Before the fix this ran exactly once and was never heard from again.
    assert starts["n"] > 1


async def test_supervise_restarts_a_worker_that_merely_returns(monkeypatch):
    # The other half, and the one no exception handler would have caught:
    # `_bridge_market_data_to_candles` ends by *returning* when its feed
    # stops yielding, which is what a dropped market-data connection looks
    # like from here.
    monkeypatch.setattr(supervision, "RESTART_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(supervision, "MAX_RESTART_DELAY_SECONDS", 0.01)
    starts = {"n": 0}
    stop_event = asyncio.Event()

    async def returns_immediately():
        starts["n"] += 1

    task = asyncio.create_task(supervision.supervise("market_data", returns_immediately, stop_event))
    await asyncio.sleep(0.2)
    stop_event.set()
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert starts["n"] > 1


async def test_supervise_stops_restarting_once_shutdown_is_requested(monkeypatch):
    # Control: this must not fight the shutdown path. SIGTERM sets
    # `stop_event`, and the supervisor has to stand down rather than
    # resurrect workers while the process is trying to exit.
    monkeypatch.setattr(supervision, "RESTART_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(supervision, "MAX_RESTART_DELAY_SECONDS", 0.01)
    starts = {"n": 0}
    stop_event = asyncio.Event()

    async def dies():
        starts["n"] += 1
        stop_event.set()
        raise RuntimeError("boom")

    await asyncio.wait_for(supervision.supervise("scanner", dies, stop_event), timeout=1.0)
    assert starts["n"] == 1


async def test_supervise_lets_cancellation_through(monkeypatch):
    """Control: `main` shuts down by cancelling these tasks, so
    `CancelledError` has to propagate rather than be caught and treated as
    a worker death to restart.

    Measured honestly: this passes with or without an explicit
    `except asyncio.CancelledError: raise`, because `CancelledError`
    derives from `BaseException` and `except Exception` never sees it --
    the explicit clause was dead code and has been removed. What this test
    does catch is someone later widening that handler to `BaseException`,
    which would make the process unkillable.
    """
    monkeypatch.setattr(supervision, "RESTART_DELAY_SECONDS", 0.01)
    stop_event = asyncio.Event()

    async def forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(supervision.supervise("scanner", forever, stop_event))
    await asyncio.sleep(0.05)
    task.cancel()

    # Bounded on purpose. A supervisor that swallows `CancelledError` never
    # finishes, so `await task` would hang the whole suite instead of
    # failing -- which is a much worse signal than an assertion.
    _done, pending = await asyncio.wait([task], timeout=1.0)
    assert not pending, "the supervisor swallowed CancelledError — the process could never shut down"
    with pytest.raises(asyncio.CancelledError):
        task.result()
