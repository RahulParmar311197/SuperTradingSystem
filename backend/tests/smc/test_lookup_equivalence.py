"""Two more hot-path scans replaced by lookups, with the results pinned.

`detect_order_blocks` asked "is there a same-direction FVG within +/-2
bars of this break?" by scanning every gap, and `detect_equal_levels`
found each anchor's price band by scanning every candidate. Both lists
grow with the series, so both were quadratic in history length -- see
docs/ARCHITECTURE.md. They are now a dict lookup and a binary search.

Neither may change what the engine reports. As with
`test_mitigation_equivalence.py`, these carry the pre-optimisation scans
as reference implementations and compare against them, because a
stash-verify proves nothing when the old code is also correct.
"""

import random
from datetime import datetime, timedelta, timezone

from app.smc.fvg import detect_fvgs
from app.smc.liquidity import detect_equal_levels
from app.smc.order_blocks import detect_order_blocks
from app.smc.structure import detect_structure_events
from app.smc.swings import detect_swings
from app.smc.types import Candle, Direction, SwingType


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
        candles.append(Candle(timestamp=t, open=o, high=h, low=l, close=c, volume=1000 + rng.randint(0, 500)))
        price, t = c, t + timedelta(minutes=1)
    return candles


def _reference_has_adjacent_fvg(event, fvgs) -> bool:
    """The pre-optimisation scan: look at every gap, every time."""
    return any(
        event.index - 2 <= fvg.created_index <= event.index + 2
        and (
            (event.direction == Direction.BULLISH and fvg.direction.value == "BULLISH")
            or (event.direction == Direction.BEARISH and fvg.direction.value == "BEARISH")
        )
        for fvg in fvgs
    )


def _reference_equal_levels(swings, tolerance_pct: float = 0.05):
    """The pre-optimisation full scan, returning (price, size) per pool so
    the comparison does not depend on LiquidityPool identity."""
    out = []
    for swing_type in (SwingType.HIGH, SwingType.LOW):
        candidates = sorted((s for s in swings if s.swing_type == swing_type), key=lambda s: s.price)
        used: set[int] = set()
        for anchor in candidates:
            if anchor.index in used:
                continue
            tolerance = anchor.price * (tolerance_pct / 100)
            group = [s for s in candidates if s.index not in used and abs(s.price - anchor.price) <= tolerance]
            if len(group) < 2:
                continue
            for s in group:
                used.add(s.index)
            out.append((swing_type.value, round(sum(s.price for s in group) / len(group), 10), len(group)))
    return out


def test_adjacent_fvg_lookup_matches_a_full_scan():
    """Routes entirely through `detect_order_blocks`.

    An earlier version of this test rebuilt the index inside the test and
    compared that to the reference -- so it was checking a copy of the
    logic, not the shipped logic, and an injected off-by-one in the real
    +/-2 window sailed straight past it. `fvg_score` contributes exactly
    0.3 to `strength`, so differencing a normal run against one given no
    gaps at all recovers the predicate's answer from the real code path.
    """
    compared = 0
    hits = 0
    for seed in range(20):
        candles = _walk(400, seed=seed, volatility=1.0 + seed % 3)
        swings = detect_swings(candles)
        events = detect_structure_events(candles, swings)
        fvgs = detect_fvgs(candles)

        with_gaps = detect_order_blocks(candles, events, fvgs)
        without_gaps = detect_order_blocks(candles, events, [])
        assert len(with_gaps) == len(without_gaps)

        by_event = {e.index: e for e in events}
        for block, bare in zip(with_gaps, without_gaps):
            event = by_event[block.caused_event_index]
            expected = _reference_has_adjacent_fvg(event, fvgs)
            delta = block.strength - bare.strength
            assert abs(delta - (0.3 if expected else 0.0)) < 1e-3, (
                f"seed={seed} event@{event.index}: expected adjacent_fvg={expected}, "
                f"strength delta={delta}"
            )
            hits += expected
            compared += 1
    assert compared > 100, f"only {compared} blocks compared"
    # Both branches must actually occur, or the assertion above is vacuous.
    assert hits > 0, "no block ever had an adjacent FVG -- the corpus proves nothing"
    assert hits < compared, "every block had an adjacent FVG -- the False branch is untested"


def test_equal_levels_match_a_full_scan():
    compared = 0
    for seed in range(25):
        candles = _walk(500, seed=seed, volatility=1.0 + seed % 3)
        swings = detect_swings(candles)
        expected = _reference_equal_levels(swings)
        actual = [
            (
                "HIGH" if p.side.value == "BUY_SIDE" else "LOW",
                round(p.price, 10),
                len(p.member_indices),
            )
            for p in detect_equal_levels(swings)
        ]
        assert actual == expected, f"seed={seed}"
        compared += len(expected)
    assert compared > 100, f"only {compared} pools compared -- the corpus is not exercising this"
