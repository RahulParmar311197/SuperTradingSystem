"""Candle aggregation / timeframe resampling (blueprint §14, §16)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market.timeframes import is_valid_upsample, timeframe_to_minutes
from app.smc.types import Candle


def bucket_start(timestamp: datetime, target_minutes: int) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    minutes_since_epoch = int((timestamp - epoch).total_seconds() // 60)
    bucket_index = minutes_since_epoch // target_minutes
    return epoch + timedelta(minutes=bucket_index * target_minutes)


def aggregate_candles(candles: list[Candle]) -> Candle:
    if not candles:
        raise ValueError("Cannot aggregate an empty candle list")
    return Candle(
        timestamp=candles[0].timestamp,
        open=candles[0].open,
        high=max(c.high for c in candles),
        low=min(c.low for c in candles),
        close=candles[-1].close,
        volume=sum(c.volume for c in candles),
    )


def resample_candles(candles: list[Candle], source_timeframe: str, target_timeframe: str) -> list[Candle]:
    """Derives higher-timeframe candles from lower-timeframe ones. Only a
    strictly higher, evenly-dividing target timeframe is supported — the
    engine should never need to fabricate detail that isn't there."""
    if not is_valid_upsample(source_timeframe, target_timeframe):
        raise ValueError(f"Cannot derive {target_timeframe} candles from {source_timeframe}")

    target_minutes = timeframe_to_minutes(target_timeframe)
    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        bucket = bucket_start(candle.timestamp, target_minutes)
        buckets.setdefault(bucket, []).append(candle)

    result = []
    for bucket_ts in sorted(buckets):
        group = sorted(buckets[bucket_ts], key=lambda c: c.timestamp)
        aggregated = aggregate_candles(group)
        result.append(
            Candle(
                timestamp=bucket_ts,
                open=aggregated.open,
                high=aggregated.high,
                low=aggregated.low,
                close=aggregated.close,
                volume=aggregated.volume,
            )
        )
    return result
