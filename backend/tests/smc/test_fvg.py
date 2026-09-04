from app.smc.fvg import detect_fvgs
from app.smc.types import FVGDirection
from tests.smc.conftest import make_candles

# candle0 high=101, candle1 displacement, candle2 low=104 -> gap between 101 and 104
BULLISH_GAP = [
    (100, 101, 99, 100),
    (100, 103, 100, 103),  # displacement candle
    (104, 106, 104, 105),
]


def test_detects_bullish_fvg():
    candles = make_candles(BULLISH_GAP)
    gaps = detect_fvgs(candles)

    assert len(gaps) == 1
    gap = gaps[0]
    assert gap.direction == FVGDirection.BULLISH
    assert gap.bottom == 101
    assert gap.top == 104


def test_fvg_mitigation_when_price_returns():
    candles = make_candles(
        BULLISH_GAP
        + [
            (105, 105, 102, 103),  # dips back into the gap, fills part of it
            (103, 103, 100, 101),  # fully fills the gap
        ]
    )
    gaps = detect_fvgs(candles)
    gap = gaps[0]

    assert gap.filled_percentage == 1.0
    assert gap.mitigated is True


def test_no_gap_when_candles_overlap():
    overlapping = [
        (100, 105, 99, 102),
        (102, 106, 101, 104),
        (104, 107, 100, 103),
    ]
    candles = make_candles(overlapping)
    assert detect_fvgs(candles) == []
