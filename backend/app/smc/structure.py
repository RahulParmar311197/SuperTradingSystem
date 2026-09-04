"""Market structure detection: BOS, CHoCH, MSS (blueprint §20-21).

Definitions used here (configurable behavior lives in the break rule, not
the labels):

* BOS  — close breaks the most recent unbroken swing *in the direction of
  the current trend* (continuation).
* CHoCH — close breaks the most recent unbroken swing *against* the
  current trend (first sign of a possible reversal).
* MSS — a CHoCH that is subsequently confirmed by a BOS in the new
  direction, before an opposite CHoCH invalidates it.

Only swings visible as of each candle (`confirmed_index <= i`) are used, so
this function is look-ahead safe as long as the caller passes a truncated
candle list.
"""

from __future__ import annotations

from app.smc.types import Direction, StructureEvent, StructureEventType, Swing, SwingType, Candle


def detect_structure_events(candles: list[Candle], swings: list[Swing]) -> list[StructureEvent]:
    events: list[StructureEvent] = []
    trend: Direction | None = None
    broken_swing_indices: set[int] = set()

    swings_by_confirmation = sorted(swings, key=lambda s: s.confirmed_index)
    visible_highs: list[Swing] = []
    visible_lows: list[Swing] = []
    swing_ptr = 0

    for i, candle in enumerate(candles):
        while swing_ptr < len(swings_by_confirmation) and swings_by_confirmation[swing_ptr].confirmed_index <= i:
            swing = swings_by_confirmation[swing_ptr]
            (visible_highs if swing.swing_type == SwingType.HIGH else visible_lows).append(swing)
            swing_ptr += 1

        close = candle.close

        if visible_highs:
            target = visible_highs[-1]
            if target.index not in broken_swing_indices and close > target.price:
                event_type = (
                    StructureEventType.BOS
                    if trend in (Direction.BULLISH, None)
                    else StructureEventType.CHOCH
                )
                events.append(
                    StructureEvent(
                        index=i,
                        timestamp=candle.timestamp,
                        event_type=event_type,
                        direction=Direction.BULLISH,
                        broken_swing_index=target.index,
                        broken_price=target.price,
                        break_price=close,
                    )
                )
                broken_swing_indices.add(target.index)
                trend = Direction.BULLISH

        if visible_lows:
            target = visible_lows[-1]
            if target.index not in broken_swing_indices and close < target.price:
                event_type = (
                    StructureEventType.BOS
                    if trend in (Direction.BEARISH, None)
                    else StructureEventType.CHOCH
                )
                events.append(
                    StructureEvent(
                        index=i,
                        timestamp=candle.timestamp,
                        event_type=event_type,
                        direction=Direction.BEARISH,
                        broken_swing_index=target.index,
                        broken_price=target.price,
                        break_price=close,
                    )
                )
                broken_swing_indices.add(target.index)
                trend = Direction.BEARISH

    return events


def promote_confirmed_shifts(events: list[StructureEvent]) -> list[StructureEvent]:
    """Emit an additional MSS event wherever a CHoCH is later confirmed by a
    same-direction BOS, before an opposite CHoCH cancels it out."""
    mss_events: list[StructureEvent] = []
    pending: StructureEvent | None = None

    for event in events:
        if event.event_type == StructureEventType.CHOCH:
            pending = event
            continue
        if event.event_type == StructureEventType.BOS and pending is not None:
            if event.direction == pending.direction:
                mss_events.append(
                    StructureEvent(
                        index=event.index,
                        timestamp=event.timestamp,
                        event_type=StructureEventType.MSS,
                        direction=event.direction,
                        broken_swing_index=event.broken_swing_index,
                        broken_price=event.broken_price,
                        break_price=event.break_price,
                    )
                )
                pending = None
            else:
                pending = None

    return mss_events
