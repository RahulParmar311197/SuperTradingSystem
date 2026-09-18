"""A heartbeat must mean "alive and subscribed", not "a trade just happened".

`heartbeat("market_data")` sat inside the tick loop of
`_bridge_market_data_to_candles`, so the key was refreshed only when a tick
arrived. Every other worker beats once per 60s loop, and
`_HEARTBEAT_TTL_SECONDS` (90s) was chosen against exactly that cadence --
its comment in app/core/redis.py enumerates scanner_worker,
auto_trade_worker and live_reconciliation, and does not mention this one.
That omission was the bug: this worker had no loop interval to name.

Measured against NSE's 03:45-10:00 UTC session, a healthy subscribed
worker was reported UP for at best **18.6% of the week** -- stale 90
seconds into every overnight (1065 silent minutes), every weekend (3945),
and any quiet stretch in a thin instrument mid-session. `GET /health` is
the endpoint an operator consults *during* an incident, and it was calling
this worker dead more often than alive.
"""

import asyncio

import pytest

from app.core.redis import _HEARTBEAT_TTL_SECONDS
from app.workers import main as workers_main

pytestmark = pytest.mark.asyncio


class _SilentFeed:
    """Subscribed and healthy, with nothing trading -- an NSE evening."""

    def __init__(self) -> None:
        self.subscribed = False

    async def subscribe(self, symbols):
        self.subscribed = True
        await asyncio.Event().wait()  # never yields, never returns
        yield  # pragma: no cover - unreachable, makes this an async generator


class _EndingFeed:
    """A dropped connection: the generator simply stops yielding."""

    def __init__(self, ticks=()):
        self.ticks = list(ticks)

    async def subscribe(self, symbols):
        for tick in self.ticks:
            yield tick


class _Worker:
    def __init__(self, feed=None, symbols=("NIFTY",)):
        self.feed = feed
        self.symbols = list(symbols)
        self.seen = []

    async def process_tick(self, tick):
        self.seen.append(tick)


def _count_beats(monkeypatch) -> list[str]:
    beats: list[str] = []

    async def fake_heartbeat(name: str) -> None:
        beats.append(name)

    monkeypatch.setattr(workers_main, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(workers_main, "MARKET_DATA_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    return beats


async def _shut_down(task: asyncio.Task, timeout: float = 2.0) -> None:
    """Cancel the bridge and insist it actually finishes.

    Injection-tested, and it took two attempts to get right. Dropping the
    beater's cancellation leaves the bridge stuck on `await beater` inside
    its own `finally`, so the task never completes its cancellation and a
    plain `await asyncio.gather(task, ...)` hangs forever rather than
    failing. A control that hangs gets killed by a CI timeout and read as
    a flake, which is worse than no control -- an earlier round learned
    exactly this and the lesson did not transfer on the first try here.
    """
    task.cancel()
    _done, pending = await asyncio.wait([task], timeout=timeout)
    assert not pending, (
        f"the bridge did not finish cancelling within {timeout}s -- it is stuck "
        "awaiting a heartbeat task that nothing cancelled"
    )


async def test_the_heartbeat_keeps_beating_through_a_silent_market(monkeypatch):
    """Behavioural proof, and the whole round. Not one tick is delivered,
    and the worker must still report itself alive."""
    beats = _count_beats(monkeypatch)
    market, candles = _Worker(_SilentFeed()), _Worker()

    task = asyncio.create_task(workers_main._bridge_market_data_to_candles(market, candles))
    await asyncio.sleep(0.15)
    await _shut_down(task)

    assert market.seen == [], "fixture must deliver no ticks at all"
    assert len(beats) > 1, (
        f"a subscribed worker in a quiet market beat {len(beats)} time(s); "
        "before this fix it beat zero and GET /health called it dead"
    )
    assert set(beats) == {"market_data"}


async def test_the_heartbeat_stops_once_the_feed_is_gone(monkeypatch):
    """Control, and the reason this is tied to the subscription rather than
    to the process. A heartbeat that outlived the thing it describes would
    be the same fabricated claim this codebase has removed from two risk
    paths -- a dead feed must still go stale within one TTL.
    """
    beats = _count_beats(monkeypatch)
    market, candles = _Worker(_EndingFeed()), _Worker()

    # Bounded deliberately. Injection-tested: dropping the cancellation
    # makes the bridge await a beater that never finishes, so an unbounded
    # call here HANGS instead of failing -- a control that hangs is a
    # control that gets killed by a CI timeout and read as flake. An
    # earlier round learned this the same way.
    await asyncio.wait_for(
        workers_main._bridge_market_data_to_candles(market, candles), timeout=2.0
    )
    settled = len(beats)
    await asyncio.sleep(0.05)  # several intervals at the patched cadence

    assert len(beats) == settled, (
        f"heartbeat kept beating after the feed ended ({settled} -> {len(beats)}); "
        "a dead worker would report itself healthy forever"
    )


async def test_a_redis_failure_does_not_end_the_heartbeat_loop(monkeypatch):
    """Control. `heartbeat` is a bare `redis.set`; an earlier round found
    one transient error ending the loop it lived in outright. The timer
    must survive it and keep trying."""
    calls = {"n": 0}

    async def flaky_heartbeat(name: str) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("Redis went away for a moment")

    monkeypatch.setattr(workers_main, "heartbeat", flaky_heartbeat)
    monkeypatch.setattr(workers_main, "MARKET_DATA_HEARTBEAT_INTERVAL_SECONDS", 0.01)
    market, candles = _Worker(_SilentFeed()), _Worker()

    task = asyncio.create_task(workers_main._bridge_market_data_to_candles(market, candles))
    await asyncio.sleep(0.1)
    await _shut_down(task)

    assert calls["n"] > 1, "the loop stopped at the first Redis error"


async def test_ticks_still_reach_both_workers(monkeypatch):
    """Control. The bridge's actual job is forwarding every tick to both
    the market-data worker and the candle worker; a heartbeat change must
    not disturb that."""
    _count_beats(monkeypatch)
    ticks = ["t1", "t2", "t3"]
    market, candles = _Worker(_EndingFeed(ticks)), _Worker()

    await asyncio.wait_for(
        workers_main._bridge_market_data_to_candles(market, candles), timeout=2.0
    )

    assert market.seen == ticks
    assert candles.seen == ticks


async def test_the_interval_stays_comfortably_inside_the_ttl():
    """Control on the invariant the TTL's own comment states, and the one a
    later change is most likely to break: an interval at or above the TTL
    reintroduces exactly the flap this fixes, and an earlier round already
    had to fix the mirror image of it (a 30s TTL against a 60s loop).
    """
    assert workers_main.MARKET_DATA_HEARTBEAT_INTERVAL_SECONDS < _HEARTBEAT_TTL_SECONDS
    assert workers_main.MARKET_DATA_HEARTBEAT_INTERVAL_SECONDS <= _HEARTBEAT_TTL_SECONDS / 1.5, (
        "leave margin for one slow pass, as the TTL comment requires"
    )
