from datetime import datetime, timedelta, timezone

import pytest

from app.market.aggregation import SESSION_OPEN_MINUTES_UTC, bucket_start, resample_candles
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


# --- naive timestamps: the two branches used to disagree -------------------


def test_both_bucket_branches_accept_a_naive_timestamp():
    """`bucket_start` gave the same input two different answers.

    The weekly branch does no timezone arithmetic and silently returned a
    bucket; the sub-weekly branch subtracts an aware epoch and raised
    `TypeError: can't subtract offset-naive and offset-aware datetimes`.
    Which behaviour you got depended only on the bucket size.
    """
    naive = datetime(2026, 9, 16, 9, 15)
    assert bucket_start(naive, 15) == datetime(2026, 9, 16, 9, 15, tzinfo=timezone.utc)
    assert bucket_start(naive, 10080) == datetime(2026, 9, 14, tzinfo=timezone.utc)


@pytest.mark.parametrize("minutes", [1, 5, 15, 30, 60, 1440, 10080])
def test_a_bucket_is_always_utc_aware(minutes):
    # Whatever went in. A naive bucket start flowing back into candle
    # timestamps is how an IST clock time reaches a UTC reader.
    assert bucket_start(datetime(2026, 9, 16, 9, 15), minutes).utcoffset() == timedelta(0)


def test_a_naive_timestamp_is_read_as_utc_not_as_machine_local_time():
    # `astimezone()` on a naive value assumes the machine's zone, which a
    # UTC-configured CI can never catch. Same convention as
    # `app.ict.killzones._utc_hour` and the Upstox candle parser.
    assert bucket_start(datetime(2026, 9, 16, 3, 45), 15) == datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc)


def test_an_aware_timestamp_is_unchanged_by_the_normalisation():
    # Control: the fix must be invisible to every caller that already
    # passes aware timestamps, which is all of production.
    aware = datetime(2026, 9, 16, 3, 50, tzinfo=timezone.utc)
    assert bucket_start(aware, 15) == datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc)


def test_resampling_naive_candles_no_longer_raises():
    candles = [
        Candle(datetime(2026, 9, 16, 3, 45) + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5, 10)
        for i in range(5)
    ]
    assert len(resample_candles(candles, "1m", "5m")) == 1


# --- the trailing bucket ---------------------------------------------------


def test_an_incomplete_trailing_bucket_is_emitted_and_looks_closed():
    """Pinning known behaviour, not endorsing it.

    Eighteen 1m candles into 15m give one 15-minute bar and one 3-minute
    bar, and nothing distinguishes the second from a finished one -- every
    `candles[-1]` downstream reads it as closed. `CandleWorker` avoids this
    with `_completes_bucket`; this function cannot, because whether more
    bars are coming is not something the input can say.

    Here so that a change to it is deliberate and visible rather than a
    silent shift under the strategy engine.
    """
    base = datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc)
    candles = [Candle(base + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5, 10) for i in range(18)]
    out = resample_candles(candles, "1m", "15m")
    assert [c.volume for c in out] == [150.0, 30.0]


# --- session alignment, now decided ----------------------------------------

_NSE_OPEN = datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc)   # 09:15 IST
_NSE_CLOSE = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)  # 15:30 IST
_INTRADAY_SIZES = (3, 5, 15, 30, 60, 120, 240)


@pytest.mark.parametrize("minutes", _INTRADAY_SIZES)
def test_the_days_first_intraday_bar_starts_at_the_session_open(minutes):
    """Every intraday size now opens its first bar of the day at 03:45 UTC.

    It did not. Epoch anchoring aligned every sub-weekly size with midnight
    UTC, which is a different claim from aligning with a session: 3m, 5m
    and 15m happened to land on the open, but 30m started 15 minutes
    before it, 1h 45 minutes before, 2h 105 and 4h 225 -- so the day's
    first "4h candle" was stamped 05:30 IST, a time no NSE instrument had
    traded in, and held 15 minutes of trading out of a nominal 240.
    """
    assert bucket_start(_NSE_OPEN, minutes) == _NSE_OPEN


def test_the_short_bar_moved_from_the_open_to_the_close():
    """An NSE session is 375 minutes, which 30/60/120/240 all fail to
    divide, so exactly one bar a day is short whichever anchor is used.
    This is the whole decision: *where* that bar sits.

    Asserted as a real count over the session rather than as a claim about
    one timestamp -- the first bar must be full, and the deficit must be at
    the end.
    """
    for minutes in (30, 60, 120, 240):
        traded: dict = {}
        cursor = _NSE_OPEN
        while cursor < _NSE_CLOSE:
            traded[bucket_start(cursor, minutes)] = traded.get(bucket_start(cursor, minutes), 0) + 1
            cursor += timedelta(minutes=1)
        spans = [count for _, count in sorted(traded.items())]
        assert spans[0] == minutes, (
            f"the first {minutes}m bar of the day holds {spans[0]} traded minutes, not {minutes}"
        )
        assert sum(spans) == 375
        assert all(s == minutes for s in spans[:-1]), spans
        assert spans[-1] <= minutes


def test_the_candle_grid_and_the_ict_session_open_agree():
    """These are two declarations of the same fact and used to disagree.

    `ICTConfig.session_open_utc` is 03:45 UTC (fixed in an earlier round,
    when it was an IST literal handed to UTC candles) and anchors the
    opening range. The candle grid anchored on midnight UTC. A strategy
    combining an opening-range condition with 30m structure was therefore
    reading two different clocks.
    """
    from app.ict.engine import ICTConfig

    session_open = ICTConfig().session_open_utc
    assert SESSION_OPEN_MINUTES_UTC == session_open.hour * 60 + session_open.minute


@pytest.mark.parametrize("minutes", [1, 3, 5, 15])
def test_the_timeframes_production_actually_uses_are_unchanged(minutes):
    """Control, and the measurement behind "this changes nothing that runs".

    `app/workers/main.py` derives only 5m and 15m and buckets ticks at 1m.
    225 divides all of those, so the session offset is a no-op for every
    timeframe in the live path -- this pins that claim instead of asserting
    it in prose. Compared against the old midnight-UTC formula directly, so
    it fails if the offset ever starts biting a live timeframe.
    """
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    for hours in range(0, 48, 7):
        for extra in (0, 1, 13, 29, 44, 59):
            ts = datetime(2026, 9, 16, tzinfo=timezone.utc) + timedelta(hours=hours, minutes=extra)
            since_epoch = int((ts - epoch).total_seconds() // 60)
            midnight_anchored = epoch + timedelta(minutes=(since_epoch // minutes) * minutes)
            assert bucket_start(ts, minutes) == midnight_anchored, ts


def test_daily_and_weekly_keep_calendar_anchoring():
    """Control. The offset is deliberately intraday-only.

    A 00:00-24:00 UTC day already contains the whole NSE session, so there
    is nothing for an offset to fix there, and shifting it would restamp
    every stored daily bar. Weekly stays on its Monday anchor, fixed in an
    earlier round when epoch arithmetic made weeks run Thursday-to-Wednesday.
    """
    assert bucket_start(_NSE_OPEN, 1440) == datetime(2026, 9, 16, tzinfo=timezone.utc)
    # 2026-09-16 is a Wednesday; its week starts on Monday the 14th.
    assert bucket_start(_NSE_OPEN, 10080) == datetime(2026, 9, 14, tzinfo=timezone.utc)


@pytest.mark.parametrize("minutes", [1, 5, 15, 30, 60, 120, 240, 1440])
def test_a_bucket_always_contains_the_timestamp_that_chose_it(minutes):
    """Control on the invariant an anchor offset is most likely to break.

    A bucket start must never be *after* its own timestamp, and never more
    than one bucket before it. An off-by-one in the offset arithmetic --
    adding it where it should be subtracted, or forgetting the floor -- is
    exactly the mistake that produces a bar timestamped in its own future,
    which nothing downstream would report and every indexed reader would
    silently believe.
    """
    base = datetime(2026, 9, 16, tzinfo=timezone.utc)
    for offset_minutes in range(0, 1440, 7):
        ts = base + timedelta(minutes=offset_minutes)
        start = bucket_start(ts, minutes)
        assert start <= ts, (ts, start)
        assert ts - start < timedelta(minutes=minutes), (ts, start)
