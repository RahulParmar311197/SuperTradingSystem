"""Shared data types for the SMC (Smart Money Concepts) engine.

Every detector here operates on a plain, ordered list of `Candle`. Callers
(replay, backtest, live scanning) are responsible for only ever passing
candles with `timestamp <= current_time` — see blueprint §45 "Look-Ahead
Prevention". The engine itself never reaches past the end of the list it is
given, so handing it a truncated history is sufficient to guarantee safety.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


class Direction(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


class SwingType(StrEnum):
    HIGH = "HIGH"
    LOW = "LOW"


class SwingLabel(StrEnum):
    HH = "HH"  # Higher High
    HL = "HL"  # Higher Low
    LH = "LH"  # Lower High
    LL = "LL"  # Lower Low
    NONE = "NONE"  # not enough prior context of the same type yet


@dataclass(slots=True)
class Swing:
    index: int
    confirmed_index: int
    timestamp: datetime
    price: float
    swing_type: SwingType
    label: SwingLabel = SwingLabel.NONE


class StructureEventType(StrEnum):
    BOS = "BOS"
    CHOCH = "CHOCH"
    MSS = "MSS"


@dataclass(slots=True)
class StructureEvent:
    index: int
    timestamp: datetime
    event_type: StructureEventType
    direction: Direction
    broken_swing_index: int
    broken_price: float
    break_price: float


class LiquiditySide(StrEnum):
    BUY_SIDE = "BUY_SIDE"  # resting above price, above swing highs / equal highs
    SELL_SIDE = "SELL_SIDE"  # resting below price, below swing lows / equal lows


class LiquiditySourceType(StrEnum):
    EQUAL_HIGHS = "EQUAL_HIGHS"
    EQUAL_LOWS = "EQUAL_LOWS"
    PREVIOUS_DAY_HIGH = "PREVIOUS_DAY_HIGH"
    PREVIOUS_DAY_LOW = "PREVIOUS_DAY_LOW"
    PREVIOUS_WEEK_HIGH = "PREVIOUS_WEEK_HIGH"
    PREVIOUS_WEEK_LOW = "PREVIOUS_WEEK_LOW"
    SESSION_HIGH = "SESSION_HIGH"
    SESSION_LOW = "SESSION_LOW"


@dataclass(slots=True)
class LiquidityPool:
    side: LiquiditySide
    source_type: LiquiditySourceType
    price: float
    formed_index: int
    formed_timestamp: datetime
    member_indices: list[int] = field(default_factory=list)
    swept: bool = False
    swept_index: int | None = None
    swept_timestamp: datetime | None = None
    rejected: bool = False  # swept and closed back inside -> rejection / sweep


class FVGDirection(StrEnum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"


@dataclass(slots=True)
class FairValueGap:
    direction: FVGDirection
    top: float
    bottom: float
    created_index: int
    created_at: datetime
    timeframe: str = ""
    mitigated: bool = False
    invalidated: bool = False
    filled_percentage: float = 0.0

    @property
    def size(self) -> float:
        return max(self.top - self.bottom, 0.0)


@dataclass(slots=True)
class OrderBlock:
    direction: Direction
    top: float
    bottom: float
    created_index: int
    created_at: datetime
    strength: float
    caused_event_index: int
    mitigated: bool = False
    mitigated_index: int | None = None


@dataclass(slots=True)
class PremiumDiscountZone:
    range_high: float
    range_low: float

    @property
    def midpoint(self) -> float:
        return (self.range_high + self.range_low) / 2

    def zone_for(self, price: float) -> str:
        if price > self.midpoint:
            return "PREMIUM"
        if price < self.midpoint:
            return "DISCOUNT"
        return "EQUILIBRIUM"
