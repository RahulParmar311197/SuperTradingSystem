from datetime import datetime, timedelta, timezone

import pytest

from app.market.aggregation import bucket_start, resample_candles
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


def test_weekly_buckets_start_on_monday_not_on_the_epochs_thursday():
    # Regression test: `bucket_start` derived every boundary from the Unix
    # epoch (`minutes_since_epoch // target_minutes`), which puts the
    # weekly boundary wherever 1970-01-01 fell -- and that was a Thursday.
    # A "1W" candle therefore ran Thursday to Wednesday: it opened
    # mid-week and carried the weekend in its middle instead of at its
    # edge. Every other week boundary in the codebase is an ISO week
    # (`app.smc.liquidity.detect_session_levels` buckets previous-week
    # levels by `isocalendar()`, and `RiskWindow.roll` resets weekly_pnl
    # the same way), so a weekly candle disagreed with the rest of the
    # system about which week it belonged to.
    week_minutes = timeframe_to_minutes("1W")
    monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
    assert monday.strftime("%a") == "Mon"

    # Every instant from Monday 00:00 through Sunday 23:59 belongs to the
    # bucket that opens on that Monday.
    for offset_hours in (0, 1, 24 * 2 + 9, 24 * 3 + 15, 24 * 6 + 23):
        ts = monday + timedelta(hours=offset_hours)
        assert bucket_start(ts, week_minutes) == monday, ts.strftime("%a %H:%M")

    # ...and the next Monday opens the next bucket, not a Thursday.
    next_monday = monday + timedelta(days=7)
    assert bucket_start(next_monday, week_minutes) == next_monday
    assert bucket_start(next_monday, week_minutes).strftime("%a") == "Mon"

    # Pins the specific pre-fix wrongness: Thursday used to open a bucket.
    thursday = monday + timedelta(days=3)
    assert bucket_start(thursday, week_minutes) == monday


def test_weekly_buckets_agree_with_iso_week_numbering():
    # The invariant that matters: two timestamps land in the same weekly
    # candle exactly when the rest of the codebase says they are in the
    # same ISO week.
    week_minutes = timeframe_to_minutes("1W")
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)  # a Thursday
    for day in range(30):
        a = start + timedelta(days=day)
        b = start + timedelta(days=day + 1)
        same_bucket = bucket_start(a, week_minutes) == bucket_start(b, week_minutes)
        same_iso_week = a.isocalendar()[:2] == b.isocalendar()[:2]
        assert same_bucket == same_iso_week, f"{a:%a %Y-%m-%d} vs {b:%a %Y-%m-%d}"


def test_resample_a_full_trading_week_into_one_weekly_candle():
    # End to end through `resample_candles`: a Monday-to-Friday run of
    # daily candles must collapse into exactly one weekly candle opening
    # on the Monday. Pre-fix this split into two, because Thursday started
    # a new bucket.
    monday = datetime(2026, 1, 5, tzinfo=timezone.utc)
    daily = [
        Candle(monday + timedelta(days=i), open=100 + i, high=105 + i, low=95 + i, close=101 + i, volume=10)
        for i in range(5)
    ]

    weekly = resample_candles(daily, "1D", "1W")

    assert len(weekly) == 1
    bar = weekly[0]
    assert bar.timestamp == monday
    assert bar.open == daily[0].open  # Monday's open, not Thursday's
    assert bar.close == daily[-1].close  # Friday's close
    assert bar.high == max(c.high for c in daily)
    assert bar.low == min(c.low for c in daily)
    assert bar.volume == sum(c.volume for c in daily)
