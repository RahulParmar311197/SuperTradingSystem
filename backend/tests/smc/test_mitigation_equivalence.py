"""`update_mitigation` stops scanning once a zone's outcome is settled.

That is a performance fix to the engine's hot loop -- it made
`SMCEngine.analyze` quadratic in history length, so a single pass cost
11ms at 500 bars and 7.5 seconds at 16000 (about eleven days of
one-minute data for one instrument), while `AutoTradeSupervisor` calls it
on a 60s loop.

A faster analysis that produces different zones would be worse than a slow
one. These tests carry a deliberately naive full-scan reference -- the
implementation as it was before the early exit -- and assert the real
implementation agrees with it on every field anything reads, across
randomised price action.
"""

import random
from datetime import datetime, timedelta, timezone

from app.smc.fvg import detect_fvgs
from app.smc.order_blocks import detect_order_blocks
from app.smc.structure import detect_structure_events
from app.smc.swings import detect_swings
from app.smc.types import Candle, Direction, FVGDirection


def _walk(n: int, seed: int, volatility: float = 1.0) -> list[Candle]:
    rng = random.Random(seed)
    candles: list[Candle] = []
    price = 100.0
    t = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for _ in range(n):
        o = price
        c = max(1.0, o + rng.uniform(-volatility, volatility))
        h = max(o, c) + abs(rng.uniform(0, volatility / 2))
        l = min(o, c) - abs(rng.uniform(0, volatility / 2))
        candles.append(Candle(timestamp=t, open=o, high=h, low=l, close=c, volume=1000))
        price, t = c, t + timedelta(minutes=1)
    return candles


def _reference_fvg_state(candles: list[Candle], gap) -> tuple[float, bool]:
    """The pre-optimisation loop: scan every later candle, never stop."""
    deepest_fill = 0.0
    invalidated = False
    for i in range(gap.created_index + 1, len(candles)):
        candle = candles[i]
        overlap_high = min(candle.high, gap.top)
        overlap_low = max(candle.low, gap.bottom)
        if overlap_high <= overlap_low:
            continue
        if gap.direction == FVGDirection.BULLISH:
            fill = gap.top - overlap_low
        else:
            fill = overlap_high - gap.bottom
        deepest_fill = max(deepest_fill, fill)
        if candle.low <= gap.bottom and candle.high >= gap.top:
            invalidated = True
    filled = min(deepest_fill / gap.size, 1.0) if gap.size else 0.0
    return filled, filled >= 1.0 or invalidated


def _reference_block_state(candles: list[Candle], block) -> tuple[float, bool, int | None]:
    deepest_fill = 0.0
    invalidated = False
    mitigated_index = None
    for i in range(block.caused_event_index + 1, len(candles)):
        candle = candles[i]
        overlap_high = min(candle.high, block.top)
        overlap_low = max(candle.low, block.bottom)
        if overlap_high <= overlap_low:
            continue
        if block.direction == Direction.BULLISH:
            fill = block.top - overlap_low
        else:
            fill = overlap_high - block.bottom
        deepest_fill = max(deepest_fill, fill)
        if candle.low <= block.bottom and candle.high >= block.top:
            invalidated = True
        if mitigated_index is None and (invalidated or (block.size and deepest_fill >= block.size)):
            mitigated_index = i
    filled = min(deepest_fill / block.size, 1.0) if block.size else 0.0
    mitigated = filled >= 1.0 or invalidated
    return filled, mitigated, mitigated_index if mitigated else None


def test_fvg_mitigation_matches_a_full_scan_across_many_walks():
    compared = 0
    for seed in range(25):
        candles = _walk(400, seed=seed, volatility=1.0 + seed % 3)
        for gap in detect_fvgs(candles):
            expected_filled, expected_mitigated = _reference_fvg_state(candles, gap)
            assert gap.filled_percentage == expected_filled, f"seed={seed} gap@{gap.created_index}"
            assert gap.mitigated == expected_mitigated, f"seed={seed} gap@{gap.created_index}"
            compared += 1
    # Guard against the assertions above passing because nothing was found.
    assert compared > 500, f"only {compared} gaps compared -- the corpus is not exercising this"


def test_order_block_mitigation_matches_a_full_scan_across_many_walks():
    compared = 0
    for seed in range(25):
        candles = _walk(400, seed=seed, volatility=1.0 + seed % 3)
        swings = detect_swings(candles)
        events = detect_structure_events(candles, swings)
        for block in detect_order_blocks(candles, events):
            expected_filled, expected_mitigated, expected_index = _reference_block_state(candles, block)
            assert block.filled_percentage == expected_filled, f"seed={seed} block@{block.caused_event_index}"
            assert block.mitigated == expected_mitigated, f"seed={seed} block@{block.caused_event_index}"
            assert block.mitigated_index == expected_index, f"seed={seed} block@{block.caused_event_index}"
            compared += 1
    assert compared > 50, f"only {compared} blocks compared -- the corpus is not exercising this"


def test_an_unfilled_gap_still_reports_partial_fill_exactly():
    # The early exit must not truncate a gap that never settles: its
    # filled_percentage has to reflect the deepest touch across the WHOLE
    # remaining series, not the first one.
    candles = _walk(300, seed=99)
    for gap in detect_fvgs(candles):
        if gap.mitigated:
            continue
        expected_filled, _ = _reference_fvg_state(candles, gap)
        assert gap.filled_percentage == expected_filled
