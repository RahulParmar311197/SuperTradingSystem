"""Supported timeframes (blueprint §16)."""

from __future__ import annotations

SUPPORTED_TIMEFRAMES: list[str] = ["1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1D", "1W"]

_UNIT_MINUTES = {"m": 1, "h": 60, "D": 1440, "W": 10080}


def timeframe_to_minutes(timeframe: str) -> int:
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    unit = timeframe[-1]
    quantity = int(timeframe[:-1])
    return quantity * _UNIT_MINUTES[unit]


def is_valid_upsample(source: str, target: str) -> bool:
    """A higher timeframe can only be derived from a strictly smaller one
    that divides it evenly (blueprint §16)."""
    source_minutes = timeframe_to_minutes(source)
    target_minutes = timeframe_to_minutes(target)
    return target_minutes > source_minutes and target_minutes % source_minutes == 0
