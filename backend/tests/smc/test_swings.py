from app.smc.swings import detect_swings, visible_swings
from app.smc.types import SwingLabel, SwingType
from tests.smc.conftest import make_candles

OHLC = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 105, 101, 104),  # swing high @ idx2, price 105
    (104, 104, 102, 103),
    (103, 103, 100, 101),
    (101, 101, 98, 99),  # swing low @ idx5, price 98
    (99, 103, 99, 102),
    (102, 107, 102, 106),
    (106, 106, 103, 104),
    (104, 104, 99, 100),
    (100, 101, 95, 96),
]


def test_detects_swing_high_and_low():
    candles = make_candles(OHLC)
    swings = detect_swings(candles, swing_length=2)

    highs = [s for s in swings if s.swing_type == SwingType.HIGH]
    lows = [s for s in swings if s.swing_type == SwingType.LOW]

    assert any(s.index == 2 and s.price == 105 for s in highs)
    assert any(s.index == 5 and s.price == 98 for s in lows)


def test_swing_is_not_visible_before_confirmation():
    candles = make_candles(OHLC)
    swings = detect_swings(candles, swing_length=2)
    swing_high = next(s for s in swings if s.index == 2)

    assert swing_high.confirmed_index == 4
    assert swing_high not in visible_swings(swings, as_of_index=3)
    assert swing_high in visible_swings(swings, as_of_index=4)


def test_labels_first_swing_of_each_type_as_none():
    candles = make_candles(OHLC)
    swings = detect_swings(candles, swing_length=2)
    first_high = next(s for s in swings if s.swing_type == SwingType.HIGH)
    first_low = next(s for s in swings if s.swing_type == SwingType.LOW)

    assert first_high.label == SwingLabel.NONE
    assert first_low.label == SwingLabel.NONE
