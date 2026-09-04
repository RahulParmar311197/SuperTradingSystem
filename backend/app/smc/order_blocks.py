"""Order block detection and scoring (blueprint §24).

An order block candidate is the last candle of the opposite color before the
displacement move that caused a structure break. Each candidate receives a
0-1 `strength` score built from displacement size, whether a Fair Value Gap
formed alongside it, and relative volume — a ranking heuristic, not a
probability of success (per the blueprint's explicit caution in §24/§82).
"""

from __future__ import annotations

from app.smc.types import Candle, Direction, FairValueGap, OrderBlock, StructureEvent


def _average_range(candles: list[Candle], end_index: int, lookback: int = 14) -> float:
    start = max(0, end_index - lookback)
    window = candles[start:end_index] or candles[: end_index + 1]
    if not window:
        return 0.0
    return sum(c.high - c.low for c in window) / len(window)


def _average_volume(candles: list[Candle], end_index: int, lookback: int = 14) -> float:
    start = max(0, end_index - lookback)
    window = candles[start:end_index] or candles[: end_index + 1]
    if not window:
        return 0.0
    return sum(c.volume for c in window) / len(window)


def detect_order_blocks(
    candles: list[Candle],
    events: list[StructureEvent],
    fvgs: list[FairValueGap] | None = None,
    lookback_candles: int = 10,
) -> list[OrderBlock]:
    fvgs = fvgs or []
    blocks: list[OrderBlock] = []

    for event in events:
        origin_index = None
        search_from = max(0, event.index - lookback_candles)
        for j in range(event.index - 1, search_from - 1, -1):
            candle = candles[j]
            is_bearish = candle.close < candle.open
            is_bullish = candle.close > candle.open
            if event.direction == Direction.BULLISH and is_bearish:
                origin_index = j
                break
            if event.direction == Direction.BEARISH and is_bullish:
                origin_index = j
                break

        if origin_index is None:
            continue

        origin = candles[origin_index]
        breakout_candle = candles[event.index]

        avg_range = _average_range(candles, event.index) or 1e-9
        displacement_score = min((breakout_candle.high - breakout_candle.low) / avg_range, 2.0) / 2.0

        has_adjacent_fvg = any(
            event.index - 2 <= fvg.created_index <= event.index + 2
            and (
                (event.direction == Direction.BULLISH and fvg.direction.value == "BULLISH")
                or (event.direction == Direction.BEARISH and fvg.direction.value == "BEARISH")
            )
            for fvg in fvgs
        )
        fvg_score = 1.0 if has_adjacent_fvg else 0.0

        avg_volume = _average_volume(candles, event.index) or 1e-9
        volume_score = min(breakout_candle.volume / avg_volume, 2.0) / 2.0 if avg_volume > 1e-9 else 0.5

        strength = round(0.5 * displacement_score + 0.3 * fvg_score + 0.2 * volume_score, 4)

        blocks.append(
            OrderBlock(
                direction=event.direction,
                top=origin.high,
                bottom=origin.low,
                created_index=origin_index,
                created_at=origin.timestamp,
                strength=strength,
                caused_event_index=event.index,
            )
        )

    update_mitigation(candles, blocks)
    return blocks


def update_mitigation(candles: list[Candle], blocks: list[OrderBlock]) -> None:
    for block in blocks:
        for i in range(block.caused_event_index + 1, len(candles)):
            candle = candles[i]
            if candle.low <= block.top and candle.high >= block.bottom:
                block.mitigated = True
                block.mitigated_index = i
                break
