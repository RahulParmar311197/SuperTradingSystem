from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class Timeframe(str, Enum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    D1 = "1D"
    W1 = "1W"


class MarketEvent(BaseModel):
    """Canonical, provider-neutral candle representation.

    Every raw payload from any market-data source or broker must be
    normalized into this shape before it reaches the SMC/strategy engines,
    per blueprint section 14 (Market Data Pipeline) / 15 (Standard Market
    Event).
    """

    symbol: str
    timeframe: Timeframe
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = Field(ge=0)
