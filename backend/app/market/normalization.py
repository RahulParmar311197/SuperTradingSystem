"""Normalizes provider-specific market data into one internal format
(blueprint §15 "Standard Market Event"). Each broker adapter supplies its
own field mapping — this module deliberately does not hardcode any
provider's payload shape, since those change over time (see blueprint
§51/§52's instruction to always consult current official API docs)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class StandardTick:
    symbol: str
    exchange: str
    market: str
    timestamp: datetime
    ltp: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: float = 0.0
    bid: float | None = None
    ask: float | None = None
    open_interest: float | None = None
    session: str | None = None

    @property
    def spread(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


def normalize_tick(
    raw: dict,
    field_map: dict[str, str],
    symbol: str,
    exchange: str,
    market: str,
    timestamp_parser=None,
) -> StandardTick:
    """`field_map` maps StandardTick field names to keys in `raw`, e.g.
    {"ltp": "last_price", "volume": "vol_traded_today", "bid": "best_bid_price"}.
    Missing optional fields are simply left as None/0.
    """

    def get(field: str, default=None):
        key = field_map.get(field)
        return raw.get(key, default) if key else default

    raw_timestamp = get("timestamp")
    if timestamp_parser is not None and raw_timestamp is not None:
        timestamp = timestamp_parser(raw_timestamp)
    elif isinstance(raw_timestamp, datetime):
        timestamp = raw_timestamp
    else:
        raise ValueError("A timestamp field mapping (and parser, if not already a datetime) is required")

    ltp = get("ltp")
    if ltp is None:
        raise ValueError("A 'ltp' field mapping is required")

    return StandardTick(
        symbol=symbol,
        exchange=exchange,
        market=market,
        timestamp=timestamp,
        ltp=float(ltp),
        open=_maybe_float(get("open")),
        high=_maybe_float(get("high")),
        low=_maybe_float(get("low")),
        close=_maybe_float(get("close")),
        volume=float(get("volume", 0.0) or 0.0),
        bid=_maybe_float(get("bid")),
        ask=_maybe_float(get("ask")),
        open_interest=_maybe_float(get("open_interest")),
        session=get("session"),
    )


def _maybe_float(value) -> float | None:
    return float(value) if value is not None else None
