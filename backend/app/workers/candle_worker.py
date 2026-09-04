"""CandleWorker (blueprint §66): aggregates incoming ticks into closed
base-timeframe candles, persists them, derives higher timeframes from
them (blueprint §16), and publishes each closed candle on `/ws/chart`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.redis import channel_name, publish
from app.database.session import async_session_factory
from app.market.aggregation import aggregate_candles
from app.market.aggregation import bucket_start as compute_bucket_start
from app.market.normalization import StandardTick
from app.market.repository import get_candles, upsert_candles
from app.market.timeframes import timeframe_to_minutes
from app.smc.types import Candle

logger = logging.getLogger("workers.candle")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(slots=True)
class _FormingCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_candle(self) -> Candle:
        return Candle(self.timestamp, self.open, self.high, self.low, self.close, self.volume)


def _completes_bucket(candle_start: datetime, base_minutes: int, target_minutes: int) -> bool:
    """True when the base-timeframe candle starting at `candle_start` is the
    last one inside its enclosing target-timeframe bucket."""
    next_start = candle_start + timedelta(minutes=base_minutes)
    minutes_since_epoch = int((next_start - _EPOCH).total_seconds() // 60)
    return minutes_since_epoch % target_minutes == 0


class CandleWorker:
    def __init__(
        self,
        instrument_ids: dict[str, uuid.UUID],
        base_timeframe: str = "1m",
        derived_timeframes: list[str] | None = None,
    ) -> None:
        self.instrument_ids = instrument_ids
        self.base_timeframe = base_timeframe
        self.base_minutes = timeframe_to_minutes(base_timeframe)
        self.derived_timeframes = derived_timeframes or []
        self._forming: dict[str, _FormingCandle] = {}

    async def process_tick(self, tick: StandardTick) -> Candle | None:
        """Feeds one tick in. Returns the candle that just closed, if any."""
        bucket_ts = compute_bucket_start(tick.timestamp, self.base_minutes)
        forming = self._forming.get(tick.symbol)
        closed: Candle | None = None

        if forming is not None and forming.timestamp != bucket_ts:
            closed = forming.to_candle()
            await self._on_candle_closed(tick.symbol, closed)
            forming = None

        if forming is None:
            forming = _FormingCandle(bucket_ts, tick.ltp, tick.ltp, tick.ltp, tick.ltp, tick.volume)
        else:
            forming.high = max(forming.high, tick.ltp)
            forming.low = min(forming.low, tick.ltp)
            forming.close = tick.ltp
            forming.volume += tick.volume

        self._forming[tick.symbol] = forming
        return closed

    async def _on_candle_closed(self, symbol: str, candle: Candle) -> None:
        instrument_id = self.instrument_ids.get(symbol)
        if instrument_id is not None:
            async with async_session_factory() as db:
                await upsert_candles(db, instrument_id, self.base_timeframe, [candle])

        await publish(
            channel_name("chart", str(instrument_id or symbol), self.base_timeframe),
            {"timestamp": candle.timestamp.isoformat(), "open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close, "volume": candle.volume},
        )

        if instrument_id is not None:
            for target_timeframe in self.derived_timeframes:
                target_minutes = timeframe_to_minutes(target_timeframe)
                if target_minutes <= self.base_minutes or not _completes_bucket(candle.timestamp, self.base_minutes, target_minutes):
                    continue
                await self._derive_timeframe(instrument_id, target_timeframe, target_minutes, candle.timestamp)

    async def _derive_timeframe(
        self, instrument_id: uuid.UUID, target_timeframe: str, target_minutes: int, as_of: datetime
    ) -> None:
        window = target_minutes // self.base_minutes
        async with async_session_factory() as db:
            lookback_start = as_of - timedelta(minutes=target_minutes * 2)
            recent = await get_candles(db, instrument_id, self.base_timeframe, start=lookback_start, end=as_of)
            recent = recent[-window:]
            if len(recent) < window:
                return
            derived = aggregate_candles(recent)
            derived = Candle(
                timestamp=compute_bucket_start(recent[0].timestamp, target_minutes),
                open=derived.open,
                high=derived.high,
                low=derived.low,
                close=derived.close,
                volume=derived.volume,
            )
            await upsert_candles(db, instrument_id, target_timeframe, [derived])

        await publish(
            channel_name("chart", str(instrument_id), target_timeframe),
            {"timestamp": derived.timestamp.isoformat(), "open": derived.open, "high": derived.high, "low": derived.low, "close": derived.close, "volume": derived.volume},
        )
