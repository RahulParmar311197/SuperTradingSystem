"""Opening range detection: high/low of the first N minutes of a session (blueprint §26)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta

from app.smc.types import Candle


@dataclass(slots=True)
class OpeningRange:
    session_date: date
    high: float
    low: float
    start_index: int
    end_index: int


def detect_opening_ranges(
    candles: list[Candle], session_open: time, duration_minutes: int = 15
) -> list[OpeningRange]:
    """`session_open` is a bare clock time, compared against each candle's
    own `.replace(hour=..., minute=...)`, so it must be expressed in the
    same timezone the candles carry.

    That contract is the caller's to keep, and `ICTConfig` used to break
    it: its default was the NSE open written in IST while every candle in
    this system arrives from Postgres in UTC. See
    `ICTConfig.session_open_utc`.
    """
    ranges: list[OpeningRange] = []
    if not candles:
        return ranges

    current_date: date | None = None
    window_end: object | None = None
    high = low = None
    start_index = 0

    for i, candle in enumerate(candles):
        ts = candle.timestamp
        session_start_dt = ts.replace(
            hour=session_open.hour, minute=session_open.minute, second=0, microsecond=0
        )
        in_new_session = ts.date() != current_date and ts >= session_start_dt
        if in_new_session:
            if current_date is not None and high is not None:
                ranges.append(OpeningRange(current_date, high, low, start_index, i - 1))
            current_date = ts.date()
            window_end = session_start_dt + timedelta(minutes=duration_minutes)
            high, low = candle.high, candle.low
            start_index = i
        elif current_date is not None and ts < window_end:
            high = max(high, candle.high)
            low = min(low, candle.low)

    if current_date is not None and high is not None:
        ranges.append(OpeningRange(current_date, high, low, start_index, len(candles) - 1))

    return ranges
