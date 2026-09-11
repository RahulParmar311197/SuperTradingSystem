"""A previous-day/week level must be sweepable by the session's first bar.

`detect_sweeps` scanned `range(pool.formed_index + 1, ...)` for every pool,
but `formed_index` does not mean the same thing for the two kinds of pool
that reach it:

* `detect_equal_levels` anchors at the pool's *last member swing*. That
  candle helped form the level, so it must be excluded -- the pool would
  otherwise sweep itself (the bug fixed in `test_pool_is_not_swept_by_its
  _own_member_swing`).
* `detect_session_levels` anchors at the *first candle of the following
  period*, "when the level becomes a resting liquidity target" per its own
  docstring. `period_high`/`period_low` are reset only after the pool is
  emitted, so that candle contributed nothing to the level and must be
  included.

Applying the equal-levels rule to both dropped the single candle most
likely to raid the prior session's extreme.

Why the existing tests could not see this. Every sweep assertion in
`tests/smc/test_liquidity.py` builds candles with `conftest.make_candles`,
which emits them one minute apart from a single `2026-01-05 09:15Z` start
-- the fixtures span 8 and 9 minutes on one date, so
`detect_session_levels` returns `[]` for both "day" and "week" and only
equal-level pools are ever swept. `detect_session_levels` has no direct
test anywhere in the suite. Fake-coverage shape (b): a fixture that
structurally cannot reach the breaking state.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.smc.engine import SMCConfig, SMCEngine
from app.smc.liquidity import detect_equal_levels, detect_session_levels, detect_sweeps
from app.smc.swings import detect_swings
from app.smc.types import Candle, LiquiditySide, LiquiditySourceType
from tests.smc.conftest import make_candles

# 15m bars from an NSE-like session open, so the day buckets are real.
BASE = datetime(2026, 1, 5, 3, 45, tzinfo=timezone.utc)


def _bars(spec: list[tuple[int, int, float, float, float, float]]) -> list[Candle]:
    """(day_offset, slot, open, high, low, close) -> candles 15m apart."""
    return [
        Candle(
            timestamp=BASE + timedelta(days=day, minutes=15 * slot),
            open=o, high=h, low=lo, close=c, volume=100.0,
        )
        for day, slot, o, h, lo, c in spec
    ]


# Day 1 runs 99..110. Day 2's FIRST bar (index 3) trades to 115 -- five points
# through the previous-day high -- and closes back at 106, below it.
_JUDAS_SWING = [
    (0, 0, 100, 110, 99, 105),
    (0, 1, 105, 108, 101, 104),
    (0, 2, 104, 107, 102, 103),
    (1, 0, 104, 115, 103, 106),
    (1, 1, 106, 108, 105, 107),
    (1, 2, 107, 109, 106, 108),
]


def _pdh(candles: list[Candle]):
    pools = detect_session_levels(candles, "day")
    detect_sweeps(candles, pools)
    return next(p for p in pools if p.source_type is LiquiditySourceType.PREVIOUS_DAY_HIGH)


def test_the_sessions_opening_bar_can_sweep_the_previous_day_high():
    # Regression test: this reported swept=False. The bar trades 5 points
    # through the level and closes below it -- the textbook stop-hunt-and-
    # reverse the previous-day level exists to describe.
    candles = _bars(_JUDAS_SWING)
    pool = _pdh(candles)

    assert pool.formed_index == 3, "the level is anchored at day 2's first bar"
    assert pool.price == 110.0
    assert pool.swept is True
    assert pool.swept_index == 3, "the raid is the opening bar itself"
    assert pool.rejected is True, "it closed back below the level"


def test_the_opening_bar_sweep_reaches_the_strategy_layer():
    # `ConditionType.LIQUIDITY_SWEEP` reads `SMCContext.recent_sweeps()`, so
    # a missed sweep is a signal that never fires. Pre-fix this list was
    # empty for the fixture above.
    context = SMCEngine(SMCConfig()).analyze(_bars(_JUDAS_SWING))

    swept = [p for p in context.recent_sweeps() if p.source_type is LiquiditySourceType.PREVIOUS_DAY_HIGH]
    assert swept, "a previous-day-high raid must be visible to the strategy engine"
    assert swept[0].swept_index == 3


def test_a_later_nick_does_not_steal_the_real_raids_attribution():
    # The subtler half: where a *later* bar also trades through the level,
    # the sweep was still recorded -- but against the wrong candle, with the
    # wrong magnitude and the wrong bar's close deciding `rejected`. Here the
    # genuine raid is index 3 (115, five points through); index 5 only nicks
    # 111.
    candles = _bars(
        [
            (0, 0, 100, 110, 99, 105),
            (0, 1, 105, 108, 101, 104),
            (0, 2, 104, 107, 102, 103),
            (1, 0, 104, 115, 103, 106),
            (1, 1, 106, 109, 105, 107),
            (1, 2, 107, 111, 106, 108),
        ]
    )
    pool = _pdh(candles)

    assert pool.swept_index == 3, "pre-fix this was 5 -- two bars and 30 minutes late"
    assert candles[pool.swept_index].high == 115.0
    # The lookback in `ConditionType.LIQUIDITY_SWEEP` is measured from this
    # index, so being late is not cosmetic: it shifts the window in which a
    # strategy is allowed to act on the sweep.
    assert candles[pool.swept_index].high - pool.price == 5.0


def test_the_opening_bar_can_sweep_the_previous_day_low_too():
    candles = _bars(
        [
            (0, 0, 100, 110, 99, 105),
            (0, 1, 105, 108, 101, 104),
            (0, 2, 104, 107, 102, 103),
            (1, 0, 103, 104, 95, 102),  # raids the 99 low, closes back above
            (1, 1, 102, 106, 101, 105),
        ]
    )
    pools = detect_session_levels(candles, "day")
    detect_sweeps(candles, pools)
    pool = next(p for p in pools if p.source_type is LiquiditySourceType.PREVIOUS_DAY_LOW)

    assert pool.side is LiquiditySide.SELL_SIDE
    assert pool.price == 99.0
    assert pool.swept is True
    assert pool.swept_index == 3
    assert pool.rejected is True


def test_a_level_untouched_by_the_new_session_stays_unswept():
    # The control: including the opening bar must not mark everything swept.
    candles = _bars(
        [
            (0, 0, 100, 110, 99, 105),
            (0, 1, 105, 108, 101, 104),
            (1, 0, 104, 107, 102, 106),  # stays strictly inside 99..110
            (1, 1, 106, 109, 103, 108),
        ]
    )
    pools = detect_session_levels(candles, "day")
    detect_sweeps(candles, pools)

    assert [p.swept for p in pools] == [False, False]


def test_a_previous_week_level_behaves_the_same_way():
    # 2026-01-05 is a Monday, so +7 days is the next ISO week.
    candles = _bars(
        [
            (0, 0, 100, 110, 99, 105),
            (0, 1, 105, 108, 101, 104),
            (7, 0, 104, 118, 103, 107),  # first bar of the new week raids 110
        ]
    )
    pools = detect_session_levels(candles, "week")
    detect_sweeps(candles, pools)
    pool = next(p for p in pools if p.source_type is LiquiditySourceType.PREVIOUS_WEEK_HIGH)

    assert pool.swept is True
    assert pool.swept_index == 2
    assert pool.rejected is True


def test_an_equal_level_pool_is_still_not_swept_by_its_own_last_member():
    # The control that guards the earlier fix this one sits next to. An
    # equal-highs pool anchors at its last member, and that member is in
    # `member_indices`, so starting the scan *at* `formed_index` must still
    # skip it rather than let the pool sweep itself.
    # The suite's own proven equal-highs fixture.
    candles = make_candles(
        [
            (100, 100, 99, 100),
            (100, 103, 100, 102),      # swing high #1 @ 103
            (102, 102, 100, 101),
            (101, 101, 98, 99),
            (99, 102, 99, 101),
            (101, 103.05, 100, 102),   # swing high #2 -- the pool's last member
            (102, 102, 99, 100),
            (100, 101, 97, 98),
            (98, 106, 98, 102),        # the genuine sweep, well after both
        ]
    )
    pools = [
        p
        for p in detect_equal_levels(detect_swings(candles, swing_length=1), tolerance_pct=0.5)
        if p.side is LiquiditySide.BUY_SIDE
    ]
    assert pools, "fixture must actually produce an equal-highs pool"
    detect_sweeps(candles, pools)

    for pool in pools:
        assert pool.formed_index in pool.member_indices
        if pool.swept:
            assert pool.swept_index not in pool.member_indices


@pytest.mark.parametrize("period", ["day", "week"])
def test_session_pools_carry_no_members_so_nothing_is_skipped(period):
    # The property the fix relies on: `detect_session_levels` never populates
    # `member_indices`, because the anchoring candle belongs to the *next*
    # period and contributed nothing to the level. If that ever changed, the
    # opening bar would start being skipped again.
    candles = _bars(_JUDAS_SWING + [(7, 0, 108, 112, 107, 110)])
    for pool in detect_session_levels(candles, period):
        assert pool.member_indices == []
        assert candles[pool.formed_index].timestamp == pool.formed_timestamp
