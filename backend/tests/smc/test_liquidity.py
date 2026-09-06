from app.smc.liquidity import detect_equal_levels, detect_sweeps
from app.smc.swings import detect_swings
from app.smc.types import LiquiditySide
from tests.smc.conftest import make_candles

# Two nearly-equal swing highs (~103), then a candle sweeps above both and
# closes back below -> classic liquidity grab + rejection.
EQUAL_HIGHS = [
    (100, 100, 99, 100),
    (100, 103, 100, 102),  # swing high #1 @ 103
    (102, 102, 100, 101),
    (101, 101, 98, 99),  # swing low
    (99, 102, 99, 101),
    (101, 103.05, 100, 102),  # swing high #2 @ 103.05 (equal to #1 within tolerance)
    (102, 102, 99, 100),
    (100, 101, 97, 98),  # swing low
    (98, 106, 98, 102),  # sweeps above both highs, closes back below -> rejection
]


def test_detects_equal_highs_pool():
    candles = make_candles(EQUAL_HIGHS)
    swings = detect_swings(candles, swing_length=1)
    pools = detect_equal_levels(swings, tolerance_pct=0.5)

    equal_high_pools = [p for p in pools if p.side == LiquiditySide.BUY_SIDE]
    assert len(equal_high_pools) == 1
    assert len(equal_high_pools[0].member_indices) == 2


def test_sweep_and_rejection_detected():
    candles = make_candles(EQUAL_HIGHS)
    swings = detect_swings(candles, swing_length=1)
    pools = detect_equal_levels(swings, tolerance_pct=0.5)
    detect_sweeps(candles, pools)

    pool = next(p for p in pools if p.side == LiquiditySide.BUY_SIDE)
    assert pool.swept is True
    assert pool.rejected is True
    # Index 8 is the candle this fixture was written around -- the one that
    # actually trades above both equal highs and closes back below. Asserting
    # only the booleans above let this pass while the engine was really
    # reporting the sweep at index 5, the pool's own second equal high.
    assert pool.swept_index == 8
    assert pool.swept_index not in pool.member_indices


# A double top where price never trades above the equal highs: swing high #1
# at 103, swing high #2 slightly higher at 103.05, and nothing afterwards
# takes either out. There is no liquidity grab here at all.
UNSWEPT_EQUAL_HIGHS = [
    (100, 100, 99, 100),
    (100, 103, 100, 102),  # swing high #1 @ 103
    (102, 102, 100, 101),
    (101, 101, 98, 99),  # swing low
    (99, 102, 99, 101),
    (101, 103.05, 100, 102),  # swing high #2 @ 103.05 -- completes the pool
    (102, 102, 99, 100),
    (100, 101, 97, 98),  # swing low
    (98, 102, 97, 99),  # stays below both highs -- no sweep
    (99, 101.5, 98, 100),
]


def test_pool_is_not_swept_by_its_own_member_swing():
    # Regression test: `detect_equal_levels` anchored each pool at its *first*
    # member, so `detect_sweeps` scanned a window that still contained the
    # pool's own later members. Since `price` is the group average, the higher
    # member swept the pool on its own candle -- the engine reported a
    # liquidity grab (and, because a swing-high candle closes below its own
    # high, a *rejection*) at the very moment the pool formed, when price had
    # never traded through the level. Blueprint §22 requires the ordering
    # pool -> sweep -> rejection; this inverted it.
    candles = make_candles(UNSWEPT_EQUAL_HIGHS)
    swings = detect_swings(candles, swing_length=1)
    pools = detect_equal_levels(swings, tolerance_pct=0.5)
    detect_sweeps(candles, pools)

    pool = next(p for p in pools if p.side == LiquiditySide.BUY_SIDE)
    # The setup this test depends on: price genuinely never took out the highs.
    assert max(c.high for c in candles) <= max(103, 103.05)
    assert pool.swept is False
    assert pool.swept_index is None
    assert pool.rejected is False


def test_pool_is_anchored_at_its_last_member():
    # The pool does not exist until the swing that makes the level "equal"
    # has printed, so a sweep can only be looked for after that candle.
    candles = make_candles(EQUAL_HIGHS)
    swings = detect_swings(candles, swing_length=1)
    pools = detect_equal_levels(swings, tolerance_pct=0.5)

    pool = next(p for p in pools if p.side == LiquiditySide.BUY_SIDE)
    assert pool.formed_index == max(pool.member_indices)
