"""Worker process entrypoint (blueprint §66). Runs as a separate process
from the API (see docker-compose.yml's `worker` service):

    python -m app.workers.main

Reads the same DATABASE_URL/REDIS_URL as the API. `WORKER_SYMBOLS` (comma
separated, e.g. "NIFTY,BANKNIFTY") selects which symbols the simulated
market data feed drives — swap `SimulatedFeed` for a real broker feed once
one is wired up (see app/brokers/dhan, app/brokers/upstox).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid

from app.core.redis import heartbeat
from app.core.supervision import supervise
from app.workers.auto_trade_worker import AutoTradeSupervisor
from app.workers.candle_worker import CandleWorker
from app.workers.market_data_worker import MarketDataWorker
from app.workers.scanner_worker import ScannerWorker

# ReconciliationWorker isn't started here: it needs the same
# OrderManager/PositionManager instances a user's live orders were placed
# through, which only exist inside the API process's memory (see
# app/api/orders.py's `_STACKS`) — a separate `worker` process has no way
# to reach them. See app/trading/live_reconciliation.py, started from
# app/main.py's lifespan instead.

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("workers.main")


# Matches the 60s loop cadence of every other worker, which is the number
# `_HEARTBEAT_TTL_SECONDS` (90s) was chosen against -- see its comment in
# app/core/redis.py, which enumerates scanner_worker, auto_trade_worker and
# live_reconciliation and does not mention this one. That omission was the
# bug: this worker had no loop interval to name, because it beat once per
# *tick*.
MARKET_DATA_HEARTBEAT_INTERVAL_SECONDS = 60.0


async def _beat_while_subscribed(stop: asyncio.Event) -> None:
    """Refreshes the `market_data` heartbeat on a timer until told to stop.

    **A heartbeat must mean "this worker is alive and subscribed", and it
    used to mean "a trade just happened."** `heartbeat("market_data")` sat
    inside the tick loop below, so the key was only refreshed when a tick
    arrived. With a 90s TTL against an NSE session that runs 03:45-10:00
    UTC, a perfectly healthy subscribed worker went stale 90 seconds into
    every overnight, every weekend, and any quiet stretch in a thin
    instrument mid-session -- reported UP for at best 18.6% of the week.

    `GET /health` is the endpoint an operator consults *during* an
    incident, and it was calling this worker dead more often than alive.
    Either they wire it to alerting and get paged every evening, or they
    learn to ignore market_data and miss the outage it exists to show.

    Tied to the subscription's lifetime rather than the process's, on
    purpose: the caller cancels this the moment the bridge returns or
    raises, so a genuinely dead feed still goes stale within one TTL. A
    heartbeat that outlived the thing it describes would be the same
    fabricated claim this codebase has now removed from two risk paths.
    """
    while not stop.is_set():
        # Guarded for the same reason as ScannerWorker.run's: `heartbeat`
        # is a bare `redis.set`, and one transient Redis error outside a
        # guard ended the loop it lived in outright.
        try:
            await heartbeat("market_data")
        except Exception:
            logger.exception("Market-data heartbeat failed (Redis unreachable?) — the loop continues")
        try:
            await asyncio.wait_for(stop.wait(), timeout=MARKET_DATA_HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


async def _bridge_market_data_to_candles(market_worker: MarketDataWorker, candle_worker: CandleWorker) -> None:
    """Runs the market data feed and forwards every tick into the candle
    worker too, so one feed subscription drives both."""
    stop = asyncio.Event()
    beater = asyncio.create_task(_beat_while_subscribed(stop), name="market_data_heartbeat")
    try:
        async for tick in market_worker.feed.subscribe(market_worker.symbols):
            try:
                await market_worker.process_tick(tick)
                await candle_worker.process_tick(tick)
            except Exception:
                logger.exception("Failed to process tick for %s", tick.symbol)
        logger.warning(
            "Market data feed for symbols=%s produced no more ticks; the bridge is returning. "
            "`supervise` will restart it after a backoff.",
            market_worker.symbols,
        )
    finally:
        # Stops the heartbeat whether the feed ended cleanly or raised, so
        # the key expires within one TTL and `GET /health` reports what is
        # actually true.
        stop.set()
        beater.cancel()
        try:
            await beater
        except asyncio.CancelledError:
            pass


def _feed_can_emit(feed, symbols: list[str]) -> bool:
    """Whether this feed could ever yield a tick for these symbols.

    Only `SimulatedFeed` can be answered statically -- it replays a dict it
    was handed, so an empty one is a provable dead end. Anything else is
    assumed live, because a real feed's silence is a fact about the market
    rather than about the object, and refusing to start it would be this
    guard causing the outage it exists to report.
    """
    from app.market.feed import SimulatedFeed

    if not isinstance(feed, SimulatedFeed):
        return True
    return any(feed.candles_by_symbol.get(symbol) for symbol in symbols)


async def main() -> None:
    symbols = [s.strip() for s in os.environ.get("WORKER_SYMBOLS", "").split(",") if s.strip()]
    instrument_ids_env = os.environ.get("WORKER_INSTRUMENT_IDS", "")  # "SYMBOL=uuid,SYMBOL2=uuid2"
    instrument_ids: dict[str, uuid.UUID] = {}
    for pair in instrument_ids_env.split(","):
        if "=" in pair:
            symbol, instrument_id = pair.split("=", 1)
            instrument_ids[symbol.strip()] = uuid.UUID(instrument_id.strip())

    if not symbols:
        logger.warning("WORKER_SYMBOLS is empty — market data / candle workers have nothing to do")

    from app.market.feed import SimulatedFeed

    # `candles_by_symbol={}` -- deliberately, and this is the whole point of
    # the guard below rather than an oversight to fix by inventing data.
    # `SimulatedFeed.subscribe` iterates `candles_by_symbol.get(symbol, [])`,
    # so an empty dict yields nothing and returns immediately. There is no
    # streaming provider to put here instead: `UpstoxMarketData` is REST
    # only (historical candles and LTP, no WebSocket), so a live feed would
    # have to be a polling client that does not exist yet.
    feed = SimulatedFeed(candles_by_symbol={}, exchange="NSE", market="EQUITY")
    market_worker = MarketDataWorker(feed, symbols)
    candle_worker = CandleWorker(instrument_ids, base_timeframe="1m", derived_timeframes=["5m", "15m"])
    scanner_worker = ScannerWorker(timeframe="15m", interval_seconds=60.0)
    auto_trade_supervisor = AutoTradeSupervisor(timeframe="15m", interval_seconds=60.0)

    stop_event = asyncio.Event()

    def _handle_stop(*_args) -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            pass  # not available on some platforms (e.g. Windows)

    # Each wrapped in `supervise` (app/core/supervision.py), so a worker
    # that dies is logged and
    # brought back rather than silently leaving the process running with
    # nothing behind it. `factory` is a callable, not a coroutine: a
    # coroutine object can only be awaited once, so restarting needs a
    # fresh one each time.
    supervised = {
        "scanner": scanner_worker.run,
        "autotrade": auto_trade_supervisor.run,
    }
    # Only supervise the bridge if it can actually produce something.
    #
    # It could not. The feed above holds no candles, so `subscribe()`
    # returned on its first step, `supervise` logged "returned
    # unexpectedly; restarting in 5s", and the process spent its whole
    # life restarting a generator that was structurally incapable of
    # yielding -- backing off to one attempt a minute, forever, while an
    # operator reading the log saw a worker that looked busy.
    #
    # Refusing to start it is the honest state, and it is not the same as
    # doing nothing: `scanner` and `autotrade` still run against whatever
    # candles the store holds (see `POST /admin/backfill`, which is how
    # real history gets in), and `market_data` correctly reads DOWN on
    # `GET /health` because nothing is beating for it.
    if _feed_can_emit(feed, symbols):
        supervised["market_data+candles"] = lambda: _bridge_market_data_to_candles(
            market_worker, candle_worker
        )
    else:
        logger.error(
            "No live market-data feed: the simulated feed holds no candles for %s, so the "
            "market_data bridge is NOT being started (it would restart forever without ever "
            "producing a tick). Historical candles can still be loaded through "
            "POST /admin/backfill; a streaming feed is not implemented -- UpstoxMarketData is "
            "REST only.",
            symbols or "any symbol",
        )
    tasks = [
        asyncio.create_task(supervise(name, factory, stop_event), name=name)
        for name, factory in supervised.items()
    ]

    await stop_event.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
