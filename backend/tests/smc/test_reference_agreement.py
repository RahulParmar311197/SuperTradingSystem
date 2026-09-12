"""Cross-checks `app.smc` against an independent implementation.

The comparison engine is the `smartmoneyconcepts` package, wrapped by
`app.smc.reference`. The point is not that the two agree everywhere --
they deliberately do not, and `app/smc/reference.py` documents which
differences are understood. The point is that the places they *must* agree
are asserted, so a regression in our detectors has something outside our
own codebase to fail against.

The last test in this file is the one that earns its keep independently of
the library: it pins blueprint §45's look-ahead guarantee, which is called
mandatory there and which nothing in the suite covered before.
"""

from __future__ import annotations

import datetime as dt
import random

import pytest

from app.smc.fvg import detect_fvgs
from app.smc.reference import reference_fvgs, reference_swings
from app.smc.swings import detect_swings
from app.smc.types import Candle

_BASE = dt.datetime(2025, 1, 1, 9, 15, tzinfo=dt.timezone.utc)
# Ten independent pseudo-random walks. Fixed seeds, so a failure is
# reproducible; ten of them, so an invariant that only holds by luck on one
# series does not slip through.
_SEEDS = list(range(1, 11))


def _series(seed: int, bars: int = 400) -> list[Candle]:
    rng = random.Random(seed)
    price = 100.0
    candles: list[Candle] = []
    for i in range(bars):
        price += rng.uniform(-1.5, 1.5)
        open_ = price
        close = price + rng.uniform(-1.0, 1.0)
        candles.append(
            Candle(
                timestamp=_BASE + dt.timedelta(minutes=5 * i),
                open=open_,
                high=max(open_, close) + rng.uniform(0.0, 1.2),
                low=min(open_, close) - rng.uniform(0.0, 1.2),
                close=close,
                volume=rng.uniform(800.0, 1500.0),
            )
        )
    return candles


@pytest.mark.parametrize("seed", _SEEDS)
def test_every_gap_the_reference_finds_is_one_we_find_too(seed):
    """Behavioural proof of a real invariant, not a snapshot.

    The library requires the middle candle to close in the gap's direction
    on top of the three-candle imbalance, so it finds strictly fewer gaps
    than we do. That makes its output a subset of ours -- and a subset
    relation is a genuine assertion: if our detector ever stops finding a
    gap an independent implementation still finds, this fails.

    Direction is checked too, so a sign flip in either engine is caught.
    """
    candles = _series(seed)
    ours = {gap.created_index: gap for gap in detect_fvgs(candles)}
    theirs = reference_fvgs(candles)

    assert theirs, "the reference found no gaps at all -- the fixture is not exercising it"

    missing = [gap.created_index for gap in theirs if gap.created_index not in ours]
    assert not missing, f"reference found gaps at {missing} that app.smc.fvg missed"

    for gap in theirs:
        assert ours[gap.created_index].direction == gap.direction, (
            f"direction disagreement at index {gap.created_index}: "
            f"ours={ours[gap.created_index].direction} reference={gap.direction}"
        )


@pytest.mark.parametrize("seed", _SEEDS)
def test_the_two_engines_never_disagree_about_high_versus_low(seed):
    """Behavioural proof. The engines legitimately report different *sets*
    of swings -- the library collapses its output to a strictly
    alternating HIGH/LOW sequence and so drops intermediate pivots -- but
    a classification the library does make must be one we also make. A
    bar the library calls a HIGH and we call only a LOW is a real
    disagreement about the data.

    Note the comparison is over (index, type) pairs, not a mapping keyed
    by index: one bar can legitimately be both. An outside bar whose high
    is the unique maximum of the window *and* whose low is the unique
    minimum is a swing high and a swing low at once, and our engine
    reports both. Keying by index silently drops one of them and
    manufactures a conflict -- seed 3 bar 28 is exactly that bar, and it
    is why this is written as a set comparison.
    """
    candles = _series(seed)
    ours = {(swing.index, swing.swing_type) for swing in detect_swings(candles, swing_length=3)}
    theirs = {(swing.index, swing.swing_type) for swing in reference_swings(candles, swing_length=3)}

    our_bars = {index for index, _ in ours}
    shared = {(index, kind) for index, kind in theirs if index in our_bars}
    assert shared, "no shared swings -- the fixture is not exercising the comparison"

    conflicts = sorted(
        (index, kind) for index, kind in shared if (index, kind) not in ours
    )
    assert not conflicts, (
        f"the reference classified {conflicts} but app.smc.swings did not report "
        "that swing type on those bars"
    )


@pytest.mark.parametrize("seed", _SEEDS[:3])
def test_our_detectors_never_retract_what_they_have_already_reported(seed):
    """Behavioural proof of blueprint §45, which calls look-ahead
    prevention mandatory and which nothing else in the suite covers.

    Feed the detectors one more candle at a time. A detector that only
    looks backwards can *add* a finding as a bar becomes confirmable, but
    it must never **retract** one: a swing or gap already handed to a live
    consumer cannot later cease to have existed. Anything that decides bar
    `i` using bar `i + 1` fails this.

    Measured on the reference implementation for contrast: over the same
    arrivals it retracts 253 swings and revises its FVG verdict on the
    last visible bar 37 times. That is why `app/smc/reference.py` is
    barred from the live path -- and why this test exercises *our*
    detectors, not its.

    New swings are additionally required to land on the bar that has just
    become confirmable (`new_index + swing_length == bars_now - 1`), which
    is precisely the relationship `Swing.confirmed_index` encodes. A
    detector reaching further back than that is reading history it should
    already have settled.
    """
    candles = _series(seed, bars=200)
    swing_length = 3

    previous_swings = {s.index for s in detect_swings(candles[:60], swing_length)}
    previous_gaps = {g.created_index for g in detect_fvgs(candles[:60])}

    for bars_now in range(61, len(candles) + 1):
        visible = candles[:bars_now]

        current_swings = {s.index for s in detect_swings(visible, swing_length)}
        retracted = previous_swings - current_swings
        assert not retracted, (
            f"swings at {sorted(retracted)} existed with {bars_now - 1} candles "
            f"visible and were withdrawn once candle {bars_now - 1} arrived"
        )
        for index in current_swings - previous_swings:
            assert index + swing_length == bars_now - 1, (
                f"swing at {index} appeared only at {bars_now} candles; a pivot is "
                f"confirmable {swing_length} bars after it prints, so this one was "
                "decided using candles that had already been seen"
            )

        current_gaps = {g.created_index for g in detect_fvgs(visible)}
        retracted_gaps = previous_gaps - current_gaps
        assert not retracted_gaps, (
            f"fair value gaps at {sorted(retracted_gaps)} were withdrawn once "
            f"candle {bars_now - 1} arrived"
        )

        previous_swings, previous_gaps = current_swings, current_gaps
