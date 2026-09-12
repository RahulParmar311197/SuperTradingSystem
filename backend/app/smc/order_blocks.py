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
    """Recomputes fill/mitigation state for each block from the candles after
    the structure break that created it. Mutates the blocks in place.

    A block is mitigated once price has traded *through* it, not the first
    time a candle grazes it. The distinction decides whether the zone is still
    tradable, because every consumer of `mitigated` -- `active_order_blocks`,
    and through it `ConditionType.ORDER_BLOCK`, the `order_block_retest` entry,
    the AI context and the chart overlay -- reads it as "still available to
    trade into", not as "untouched since it formed".

    Tripping on first overlap made those two readings contradict each other
    and left `order_block_retest` unfillable by construction: the entry is the
    block's midpoint, so any candle satisfying the engines' fill gate
    (`low <= entry <= high`) necessarily overlaps the block, and `SMCEngine.
    analyze` re-runs this function over the current bar before the strategy is
    evaluated. The bar that could fill the retest was always the bar that had
    just removed the block from `active_order_blocks()`. A merely adjacent bar
    killed it even sooner.

    This is the grading `app.smc.fvg.update_mitigation` already applies to the
    sibling zone type, and the direction convention is the same: a bullish
    block is demand below price, filled downward from its top; a bearish block
    is supply above price, filled upward from its bottom.
    """
    for block in blocks:
        deepest_fill = 0.0
        for i in range(block.caused_event_index + 1, len(candles)):
            candle = candles[i]
            overlap_high = min(candle.high, block.top)
            overlap_low = max(candle.low, block.bottom)
            if overlap_high <= overlap_low:
                continue

            if block.direction == Direction.BULLISH:
                fill_depth = block.top - overlap_low
            else:
                fill_depth = overlap_high - block.bottom
            deepest_fill = max(deepest_fill, fill_depth)

            if candle.low <= block.bottom and candle.high >= block.top:
                block.invalidated = True

            if block.mitigated_index is None and (
                block.invalidated or (block.size and deepest_fill >= block.size)
            ):
                block.mitigated_index = i
                # Settled: `filled_percentage` clamps at 1.0 and cannot
                # move, `mitigated` is true from here whatever follows, and
                # `mitigated_index` records only the first such bar. See
                # `app.smc.fvg.update_mitigation` for why stopping here
                # matters -- this pair of loops made `SMCEngine.analyze`
                # quadratic in history length.
                break

        block.filled_percentage = min(deepest_fill / block.size, 1.0) if block.size else 0.0
        block.mitigated = block.filled_percentage >= 1.0 or block.invalidated
        if not block.mitigated:
            block.mitigated_index = None
