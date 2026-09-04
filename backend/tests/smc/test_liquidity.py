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
