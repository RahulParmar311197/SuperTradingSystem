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


async def _bridge_market_data_to_candles(market_worker: MarketDataWorker, candle_worker: CandleWorker) -> None:
    """Runs the market data feed and forwards every tick into the candle
    worker too, so one feed subscription drives both."""
    async for tick in market_worker.feed.subscribe(market_worker.symbols):
        try:
            await market_worker.process_tick(tick)
            await candle_worker.process_tick(tick)
        except Exception:
            logger.exception("Failed to process tick for %s", tick.symbol)
        # Guarded for the same reason as ScannerWorker.run's: `heartbeat`
        # is a bare `redis.set`, and one transient Redis error outside a
        # guard ended this generator loop outright.
        try:
            await heartbeat("market_data")
        except Exception:
            logger.exception("Market-data heartbeat failed (Redis unreachable?) — the loop continues")
    logger.warning(
        "Market data feed for symbols=%s produced no more ticks; the bridge is returning. "
        "`_supervise` will restart it after a backoff.",
        market_worker.symbols,
    )


# How long `_supervise` waits before restarting a task that ended, and the
# ceiling it backs off to. Deliberately not zero: `SimulatedFeed` with no
# loaded candles returns immediately (the shipped dev default, see `main`
# below), and a zero-delay restart would spin that into a hot loop.
_RESTART_DELAY_SECONDS = 5.0
_MAX_RESTART_DELAY_SECONDS = 60.0


async def _supervise(name: str, factory, stop_event: asyncio.Event) -> None:
    """Keeps one worker coroutine running until shutdown.

    `main` used to `asyncio.create_task` each worker and then sit in
    `await stop_event.wait()`, which is only ever set by SIGINT/SIGTERM. A
    task that *ended* -- raised, or simply returned -- was never noticed:
    the process stayed alive and the container stayed up while the worker
    was gone, and at shutdown `gather(..., return_exceptions=True)`
    collected the exception and discarded it, so the cause was never even
    logged. For unattended autonomous trading (§54) and worker health
    (§117), a supervisor that cannot tell a running worker from a dead one
    is worse than no supervisor.

    Restarting in-process rather than exiting is deliberate:
    docker-compose.yml sets no `restart:` policy on any service, so exiting
    would turn a recoverable fault into a permanent outage.
    """
    delay = _RESTART_DELAY_SECONDS
    while not stop_event.is_set():
        try:
            await factory()
        # Deliberately `Exception`, never `BaseException`: `main` shuts
        # these tasks down by cancelling them, and `asyncio.CancelledError`
        # derives from `BaseException` precisely so a broad handler like
        # this one cannot swallow it. Widening this would turn every
        # shutdown into a worker "death" to be restarted, and the process
        # would never exit.
        except Exception:
            logger.exception("Worker %s died; restarting in %.0fs", name, delay)
        else:
            logger.warning("Worker %s returned unexpectedly; restarting in %.0fs", name, delay)
        if stop_event.is_set():
            break
        await asyncio.sleep(delay)
        delay = min(delay * 2, _MAX_RESTART_DELAY_SECONDS)


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

    # Each wrapped in `_supervise`, so a worker that dies is logged and
    # brought back rather than silently leaving the process running with
    # nothing behind it. `factory` is a callable, not a coroutine: a
    # coroutine object can only be awaited once, so restarting needs a
    # fresh one each time.
    supervised = {
        "market_data+candles": lambda: _bridge_market_data_to_candles(market_worker, candle_worker),
        "scanner": scanner_worker.run,
        "autotrade": auto_trade_supervisor.run,
    }
    tasks = [
        asyncio.create_task(_supervise(name, factory, stop_event), name=name)
        for name, factory in supervised.items()
    ]

    await stop_event.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
