"""The opening range must be the session's opening bars.

`detect_opening_ranges` takes a bare clock time and compares it against
each candle's own `.replace(hour=…, minute=…)`, so `session_open` has to be
expressed in whatever timezone the candles carry. That contract is the
caller's to keep, and `ICTConfig` broke it: the default was `time(9, 15)`
-- the NSE open, written in IST -- while every candle in this system comes
from Postgres, where the column is `TIMESTAMP WITH TIME ZONE` and values
arrive in UTC.

Measured on one NSE day of 5-minute candles stamped in UTC, with the
opening fifteen minutes deliberately made the day's extremes:

    ICTConfig().session_open = 09:15:00
      detected opening range: high=150.0 low=140.0
         starts at 2026-01-05T09:15:00+00:00 = 14:45 IST
      the real opening 15 minutes: high=200.0 low=100.0 starting 09:15 IST

Every `ICTConfig()` in `app/` takes the default -- none of the five
construction sites passes this field -- so every opening range the system
produced was anchored five and a half hours late.

**Severity: reporting, not a trade gate.** `opening_ranges` is read by
`app/api/charts.py`'s ICT serialiser (shared with replay analysis) and by
the AI proposal context. No `ConditionType` reads it, unlike the kill
zones. It was a wrong number on a chart and a wrong input to an AI
suggestion, not a wrong entry.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

from app.ict.engine import ICTConfig, ICTEngine
from app.ict.opening_range import detect_opening_ranges
from app.smc.types import Candle

_IST = timezone(timedelta(hours=5, minutes=30))
_OPEN_IST = datetime(2026, 1, 5, 9, 15, tzinfo=_IST)

# The opening fifteen minutes are the day's extremes, so a range that
# misses them is unmistakable rather than merely different.
_OPENING_HIGH, _OPENING_LOW = 200.0, 100.0
_REST_HIGH, _REST_LOW = 150.0, 140.0


def _nse_day_in_utc() -> list[Candle]:
    candles = []
    for i in range(75):  # 09:15 -> 15:30 IST in 5-minute candles
        ist = _OPEN_IST + timedelta(minutes=5 * i)
        opening = i < 3
        candles.append(
            Candle(
                timestamp=ist.astimezone(timezone.utc),
                open=145.0,
                high=_OPENING_HIGH if opening else _REST_HIGH,
                low=_OPENING_LOW if opening else _REST_LOW,
                close=145.0,
                volume=10.0,
            )
        )
    return candles


def test_the_default_session_open_finds_the_real_opening_bars():
    """Behavioural proof, through the default every caller actually gets."""
    candles = _nse_day_in_utc()
    config = ICTConfig()
    ranges = detect_opening_ranges(candles, config.session_open_utc, config.opening_range_minutes)

    assert len(ranges) == 1
    assert (ranges[0].high, ranges[0].low) == (_OPENING_HIGH, _OPENING_LOW), (
        "the opening range missed the session's first bars"
    )
    first = candles[ranges[0].start_index].timestamp
    assert first == _OPEN_IST, f"range starts at {first.astimezone(_IST)}, not the session open"


def test_the_engine_reports_that_same_range():
    """Behavioural proof one layer up: `ICTContext.current_opening_range`
    is what `app/api/charts.py` serialises and what the AI proposal
    context carries."""
    context = ICTEngine().analyze(_nse_day_in_utc())

    assert context.current_opening_range is not None
    assert (context.current_opening_range.high, context.current_opening_range.low) == (
        _OPENING_HIGH,
        _OPENING_LOW,
    )


def test_the_default_is_the_nse_open_expressed_in_utc():
    """Control on the constant itself, stated as the equality that makes
    it correct rather than as the literal -- so a future edit that changes
    the number has to change the meaning too."""
    assert ICTConfig().session_open_utc == _OPEN_IST.astimezone(timezone.utc).time()


def test_a_session_open_in_the_wrong_zone_finds_the_wrong_bars():
    """Control, and the measurement that made this a defect: the function
    is not broken, its default was. Handing it the IST clock time against
    UTC candles -- exactly what `ICTConfig` used to do -- picks ordinary
    mid-session bars, and this test exists so the old default cannot
    quietly come back.
    """
    candles = _nse_day_in_utc()
    ranges = detect_opening_ranges(candles, time(9, 15), 15)

    assert len(ranges) == 1
    assert (ranges[0].high, ranges[0].low) == (_REST_HIGH, _REST_LOW)
    assert candles[ranges[0].start_index].timestamp.astimezone(_IST).strftime("%H:%M") == "14:45"


def test_ist_stamped_candles_still_need_an_ist_session_open():
    """Control on the function's actual contract: the clock time is
    compared in the candle's own zone, so the same day expressed in IST
    wants the IST open. This is what makes the fix a change of default
    rather than a change of behaviour."""
    candles = [
        Candle(
            timestamp=_OPEN_IST + timedelta(minutes=5 * i),
            open=145.0,
            high=_OPENING_HIGH if i < 3 else _REST_HIGH,
            low=_OPENING_LOW if i < 3 else _REST_LOW,
            close=145.0,
            volume=10.0,
        )
        for i in range(75)
    ]
    ranges = detect_opening_ranges(candles, time(9, 15), 15)

    assert len(ranges) == 1
    assert (ranges[0].high, ranges[0].low) == (_OPENING_HIGH, _OPENING_LOW)
