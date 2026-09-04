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

from app.workers.auto_trade_worker import AutoTradeSupervisor
from app.workers.candle_worker import CandleWorker
from app.workers.market_data_worker import MarketDataWorker
from app.workers.scanner_worker import ScannerWorker

# ReconciliationWorker isn't started here: it runs per live-broker account
# (it needs that account's authenticated Broker instance), and there is no
# live broker connected to any account yet. Instantiate one per account
# once app/brokers/upstox or app/brokers/dhan is wired to real credentials.

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

    tasks = [
        asyncio.create_task(_bridge_market_data_to_candles(market_worker, candle_worker), name="market_data+candles"),
        asyncio.create_task(scanner_worker.run(), name="scanner"),
        asyncio.create_task(auto_trade_supervisor.run(), name="autotrade"),
    ]

    await stop_event.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
