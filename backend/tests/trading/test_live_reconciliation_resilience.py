"""The API process had the same heartbeat bug the workers did.

Round 122 guarded `heartbeat` in `ScannerWorker.run`,
`AutoTradeSupervisor.run` and the market-data bridge, and missed this one:
`app/trading/live_reconciliation.py` had the identical unguarded
`await heartbeat("reconciliation")` inside its `while True`. Measured, one
`ConnectionError` ended the loop after a single pass.

This copy is worse than the worker ones on three counts. It runs inside
the API process, so nothing looked ill afterwards -- the API kept serving
and `GET /health` kept reporting the process up. What died is §75's
order-divergence safety net, the thing that halts an account when its
broker state stops matching the local journal. And the dead task then
broke shutdown: `await reconciliation_task` re-raises the stored
`ConnectionError`, which `except asyncio.CancelledError: pass` does not
catch, so it escaped lifespan teardown and skipped the engine/redis
disposal that exists to stop connections leaking.
"""

import asyncio

import pytest

import app.core.supervision as supervision
import app.trading.live_reconciliation as live_reconciliation


async def _run_briefly(coro_fn, seconds: float = 0.2) -> bool:
    """True if the loop was still running when we stopped it ourselves."""
    try:
        await asyncio.wait_for(coro_fn(), timeout=seconds)
    except asyncio.TimeoutError:
        return True
    return False


# --- the bug -------------------------------------------------------------


async def test_a_transient_redis_error_in_the_heartbeat_does_not_end_reconciliation(monkeypatch):
    passes = {"n": 0}

    async def reconcile():
        passes["n"] += 1

    async def failing_heartbeat(worker_name: str):
        raise ConnectionError("Redis went away for a moment")

    monkeypatch.setattr(live_reconciliation, "reconcile_all_connected_accounts", reconcile)
    monkeypatch.setattr(live_reconciliation, "heartbeat", failing_heartbeat)

    assert await _run_briefly(lambda: live_reconciliation.run(interval_seconds=0.0)) is True
    # And it kept reconciling, rather than merely not crashing.
    assert passes["n"] > 1


async def test_a_failing_reconciliation_pass_still_heartbeats_and_keeps_looping(monkeypatch):
    # Control: the existing semantics must survive. A pass that raises is
    # already tolerated, and the heartbeat still fires -- it means "this
    # loop is alive", which it is.
    beats = {"n": 0}

    async def reconcile():
        raise RuntimeError("this pass failed")

    async def counting_heartbeat(worker_name: str):
        beats["n"] += 1

    monkeypatch.setattr(live_reconciliation, "reconcile_all_connected_accounts", reconcile)
    monkeypatch.setattr(live_reconciliation, "heartbeat", counting_heartbeat)

    assert await _run_briefly(lambda: live_reconciliation.run(interval_seconds=0.0)) is True
    assert beats["n"] > 1


# --- supervise is shared, and takes no stop_event in the API process -----


async def test_supervise_restarts_without_a_stop_event(monkeypatch):
    # The API process has no SIGINT/SIGTERM event to pass: `lifespan`
    # shuts the task down by cancelling it. `stop_event` therefore has to
    # be optional, and omitting it must not stop the restart loop.
    monkeypatch.setattr(supervision, "RESTART_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(supervision, "MAX_RESTART_DELAY_SECONDS", 0.01)
    starts = {"n": 0}

    async def dies():
        starts["n"] += 1
        raise ConnectionError("Redis went away for a moment")

    task = asyncio.create_task(supervision.supervise("live_reconciliation", dies))
    await asyncio.sleep(0.2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert starts["n"] > 1


async def test_supervise_without_a_stop_event_still_cancels(monkeypatch):
    # Control: with no stop_event, cancellation is the *only* way out, so
    # it had better work. Bounded rather than a bare `await`: a supervisor
    # that swallowed cancellation would hang the suite instead of failing.
    monkeypatch.setattr(supervision, "RESTART_DELAY_SECONDS", 0.01)

    async def forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(supervision.supervise("live_reconciliation", forever))
    await asyncio.sleep(0.05)
    task.cancel()

    _done, pending = await asyncio.wait([task], timeout=1.0)
    assert not pending, "the supervisor swallowed CancelledError — the API could never shut down"
    with pytest.raises(asyncio.CancelledError):
        task.result()


# --- teardown must not be where a dead task first surfaces ---------------


async def test_awaiting_a_task_that_already_died_reraises_its_own_error_not_cancelled():
    """Pins why `except asyncio.CancelledError: pass` alone was not enough.

    This is a property of asyncio, not of our code, and it is the one that
    made the lifespan teardown skip its engine/redis disposal: cancelling
    a task that has *already finished* does nothing, and awaiting it
    re-raises whatever it stored.
    """

    async def dies():
        raise ConnectionError("Redis went away for a moment")

    task = asyncio.create_task(dies())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(ConnectionError):
        await task


async def test_lifespan_teardown_survives_a_reconciliation_task_that_died(monkeypatch):
    """The fix, exercised through the real lifespan.

    A first version of this test planted its own dead task alongside the
    lifespan's and asserted teardown completed. Injection showed it passed
    with the `except Exception` clause removed -- it was vacuous, because
    lifespan awaits the task *it* created, not one standing next to it.
    This version replaces `supervise` itself, so the task lifespan holds is
    the dead one.
    """
    import app.main as app_main

    async def dies_immediately(name, factory, stop_event=None):
        raise ConnectionError("Redis went away for a moment")

    monkeypatch.setattr(app_main, "supervise", dies_immediately)

    async with app_main.lifespan(None):
        # Let the supervised task die while the app is "serving".
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    # Completing the `async with` *is* the assertion. Before the fix, the
    # dead task's stored ConnectionError was re-raised by
    # `await reconciliation_task`, `except asyncio.CancelledError` did not
    # catch it, and it propagated out of teardown -- skipping the
    # engine/redis disposal that follows it, which exists to stop
    # connections leaking. No `caplog` assertion here on purpose: the app
    # configures its own logging, and pinning the log line would test that
    # configuration rather than this behaviour.
