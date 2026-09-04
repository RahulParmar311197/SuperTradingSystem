from enum import Enum

from pydantic import BaseModel

from app.market.models import MarketEvent


class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


class SwingLabel(str, Enum):
    """Structure classification relative to the prior swing of the same kind."""

    HH = "HH"  # higher high
    HL = "HL"  # higher low
    LH = "LH"  # lower high
    LL = "LL"  # lower low


class Swing(BaseModel):
    index: int
    kind: SwingKind
    price: float
    candle: MarketEvent
    label: SwingLabel | None = None


class StructureDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class BreakOfStructure(BaseModel):
    """A confirmed Break of Structure event (blueprint section 20)."""

    direction: StructureDirection
    broken_swing: Swing
    breaking_index: int
    breaking_candle: MarketEvent
