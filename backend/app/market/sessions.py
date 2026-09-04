"""Market trading-session calendars (blueprint §14 "sessions")."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(slots=True)
class TradingSession:
    market: str
    open_time: time
    close_time: time
    timezone_name: str  # informational; timestamps are expected in UTC already


# A starting set; extend as more markets/brokers are integrated.
TRADING_SESSIONS: dict[str, TradingSession] = {
    "NSE": TradingSession("NSE", time(3, 45), time(10, 0), "UTC"),  # 09:15-15:30 IST in UTC
    "CRYPTO": TradingSession("CRYPTO", time(0, 0), time(23, 59, 59), "UTC"),
}


def is_market_open(market: str, timestamp: datetime) -> bool:
    session = TRADING_SESSIONS.get(market)
    if session is None:
        raise ValueError(f"Unknown market: {market}")
    current = timestamp.time()
    return session.open_time <= current <= session.close_time
