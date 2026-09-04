from app.market.models import MarketEvent

from .models import BreakOfStructure, StructureDirection, Swing, SwingKind
from .swings import detect_swings


def detect_bos(candles: list[MarketEvent], swing_length: int = 3) -> list[BreakOfStructure]:
    """Detect confirmed Break of Structure events (blueprint section 20).

    Bullish BOS: a candle closes above the most recent *confirmed* swing
    high. Bearish BOS: a candle closes below the most recent confirmed
    swing low.

    Look-ahead safety: a swing is only usable for BOS detection once it is
    confirmed (index + swing_length candles have elapsed), and a candle at
    index j is only ever compared against swings confirmed at or before j.
    This mirrors how the engine must behave during replay/live processing,
    where future candles are not yet available.
    """

    swings = detect_swings(candles, swing_length)
    swings_by_confirmation = sorted(swings, key=lambda s: s.index + swing_length)

    events: list[BreakOfStructure] = []
    last_high: Swing | None = None
    last_low: Swing | None = None
    pos = 0

    for j, candle in enumerate(candles):
        while pos < len(swings_by_confirmation) and swings_by_confirmation[pos].index + swing_length <= j:
            swing = swings_by_confirmation[pos]
            if swing.kind is SwingKind.HIGH:
                last_high = swing
            else:
                last_low = swing
            pos += 1

        if last_high is not None and last_high.index < j and candle.close > last_high.price:
            events.append(
                BreakOfStructure(
                    direction=StructureDirection.BULLISH,
                    broken_swing=last_high,
                    breaking_index=j,
                    breaking_candle=candle,
                )
            )
            last_high = None

        if last_low is not None and last_low.index < j and candle.close < last_low.price:
            events.append(
                BreakOfStructure(
                    direction=StructureDirection.BEARISH,
                    broken_swing=last_low,
                    breaking_index=j,
                    breaking_candle=candle,
                )
            )
            last_low = None

    return events
