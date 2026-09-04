from datetime import datetime, timedelta, timezone

import pytest

from app.market.aggregation import resample_candles
from app.market.timeframes import is_valid_upsample, timeframe_to_minutes
from app.smc.types import Candle


def _one_minute_candles(n: int, start: datetime) -> list[Candle]:
    candles = []
    price = 100.0
    for i in range(n):
        candles.append(
            Candle(
                timestamp=start + timedelta(minutes=i),
                open=price,
                high=price + 1,
                low=price - 1,
                close=price + 0.5,
                volume=10,
            )
        )
        price += 0.5
    return candles


def test_timeframe_to_minutes():
    assert timeframe_to_minutes("1m") == 1
    assert timeframe_to_minutes("15m") == 15
    assert timeframe_to_minutes("1h") == 60
    assert timeframe_to_minutes("1D") == 1440


def test_is_valid_upsample_rules():
    assert is_valid_upsample("1m", "5m") is True
    assert is_valid_upsample("5m", "1m") is False  # can't derive lower from higher
    assert is_valid_upsample("15m", "1h") is True


def test_resample_five_one_minute_candles_into_one_five_minute_candle():
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)  # aligned to a 5-min boundary
    candles = _one_minute_candles(5, start)

    resampled = resample_candles(candles, "1m", "5m")

    assert len(resampled) == 1
    bucket = resampled[0]
    assert bucket.open == candles[0].open
    assert bucket.close == candles[-1].close
    assert bucket.high == max(c.high for c in candles)
    assert bucket.low == min(c.low for c in candles)
    assert bucket.volume == sum(c.volume for c in candles)


def test_resample_rejects_downsampling():
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = _one_minute_candles(5, start)
    with pytest.raises(ValueError):
        resample_candles(candles, "5m", "1m")
