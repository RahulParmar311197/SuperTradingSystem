"""Market data feed abstraction (blueprint §14). A real feed wraps a
broker's WebSocket/REST market data; `SimulatedFeed` replays historical
candles as ticks so the rest of the system (and local dev) doesn't need a
live broker connection to run."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.market.normalization import StandardTick
from app.smc.types import Candle


class MarketDataFeed(ABC):
    @abstractmethod
    async def subscribe(self, symbols: list[str]) -> AsyncIterator[StandardTick]: ...

    @abstractmethod
    async def unsubscribe(self, symbols: list[str]) -> None: ...


class SimulatedFeed(MarketDataFeed):
    """Replays a pre-loaded candle history as ticks, one per candle close,
    optionally paced by `delay_seconds` between ticks (0 = as fast as
    possible, useful in tests and backtest-adjacent dev loops)."""

    def __init__(self, candles_by_symbol: dict[str, list[Candle]], exchange: str, market: str, delay_seconds: float = 0.0) -> None:
        self.candles_by_symbol = candles_by_symbol
        self.exchange = exchange
        self.market = market
        self.delay_seconds = delay_seconds
        self._active_symbols: set[str] = set()

    async def subscribe(self, symbols: list[str]) -> AsyncIterator[StandardTick]:
        self._active_symbols.update(symbols)
        for symbol in symbols:
            for candle in self.candles_by_symbol.get(symbol, []):
                if symbol not in self._active_symbols:
                    break
                yield StandardTick(
                    symbol=symbol,
                    exchange=self.exchange,
                    market=self.market,
                    timestamp=candle.timestamp,
                    ltp=candle.close,
                    open=candle.open,
                    high=candle.high,
                    low=candle.low,
                    close=candle.close,
                    volume=candle.volume,
                )
                if self.delay_seconds > 0:
                    await asyncio.sleep(self.delay_seconds)

    async def unsubscribe(self, symbols: list[str]) -> None:
        self._active_symbols.difference_update(symbols)
