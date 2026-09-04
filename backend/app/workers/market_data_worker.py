"""MarketDataWorker (blueprint §66): consumes a `MarketDataFeed`, updates
the Redis latest-price cache, and fans each tick out over `/ws/market`.

Split into `process_tick` (single, directly testable step) and `run` (the
actual infinite loop) so tests never need to drive a live feed forever.
"""

from __future__ import annotations

import logging

from app.core.redis import channel_name, publish, set_latest_price
from app.market.feed import MarketDataFeed
from app.market.normalization import StandardTick

logger = logging.getLogger("workers.market_data")


class MarketDataWorker:
    def __init__(self, feed: MarketDataFeed, symbols: list[str]) -> None:
        self.feed = feed
        self.symbols = symbols

    async def process_tick(self, tick: StandardTick) -> None:
        await set_latest_price(tick.symbol, tick.ltp)
        await publish(
            channel_name("market", tick.symbol),
            {
                "symbol": tick.symbol,
                "timestamp": tick.timestamp.isoformat(),
                "ltp": tick.ltp,
                "open": tick.open,
                "high": tick.high,
                "low": tick.low,
                "close": tick.close,
                "volume": tick.volume,
            },
        )

    async def run(self) -> None:
        logger.info("MarketDataWorker starting for symbols=%s", self.symbols)
        async for tick in self.feed.subscribe(self.symbols):
            try:
                await self.process_tick(tick)
            except Exception:
                logger.exception("Failed to process tick for %s", tick.symbol)
