from app.smc.models import StructureDirection
from app.smc.structure import detect_bos
from tests.test_smc_swings import make_candles


def test_detects_bullish_bos():
    highs = [10, 15, 11, 9, 8, 20]
    lows = [9, 13, 9, 7, 6, 18]
    closes = [9.5, 14, 10, 8, 7, 19]
    candles = make_candles(highs, lows, closes)

    events = detect_bos(candles, swing_length=1)

    assert len(events) == 1
    event = events[0]
    assert event.direction is StructureDirection.BULLISH
    assert event.broken_swing.price == 15
    assert event.breaking_index == 5


def test_detects_bearish_bos():
    highs = [22, 21, 23, 19, 18, 10]
    lows = [20, 15, 19, 17, 16, 8]
    closes = [21, 16, 20, 18, 17, 9]
    candles = make_candles(highs, lows, closes)

    events = detect_bos(candles, swing_length=1)

    bearish = [e for e in events if e.direction is StructureDirection.BEARISH]
    assert len(bearish) == 1
    assert bearish[0].broken_swing.price == 15
    assert bearish[0].breaking_index == 5


def test_no_bos_without_a_break():
    # Range-bound: no candle ever closes beyond a confirmed swing.
    highs = [10, 12, 10, 11, 10, 11]
    lows = [9, 10, 8, 9, 8, 9]
    closes = [9.5, 11, 9, 10, 9, 10]
    candles = make_candles(highs, lows, closes)

    events = detect_bos(candles, swing_length=1)

    assert events == []


def test_bos_ignores_a_swing_before_it_is_confirmed():
    # With swing_length=2, the swing high at index 2 (price 12) is only
    # confirmed once index 4 is reached. Index 3 closes above 12 *before*
    # that confirmation, so it must NOT be treated as a break — only the
    # later close above 12 (index 6, after confirmation) may count.
    highs = [8, 10, 12, 9, 8, 11, 20]
    lows = [7, 9, 10, 7, 6, 9, 18]
    closes = [7.5, 9.5, 11, 15, 7.5, 10, 19]
    candles = make_candles(highs, lows, closes)

    events = detect_bos(candles, swing_length=2)

    bullish = [e for e in events if e.direction is StructureDirection.BULLISH]
    assert len(bullish) == 1
    assert bullish[0].breaking_index == 6
    assert bullish[0].broken_swing.price == 12
