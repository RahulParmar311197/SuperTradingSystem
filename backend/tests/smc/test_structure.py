from app.smc.structure import detect_structure_events, promote_confirmed_shifts
from app.smc.swings import detect_swings
from app.smc.types import Direction, StructureEventType
from tests.smc.conftest import make_candles
from tests.smc.test_swings import OHLC


def test_bullish_bos_when_no_prior_trend():
    candles = make_candles(OHLC)
    swings = detect_swings(candles, swing_length=2)
    events = detect_structure_events(candles, swings)

    bos = next(e for e in events if e.event_type == StructureEventType.BOS)
    assert bos.direction == Direction.BULLISH
    assert bos.broken_price == 105
    assert bos.index == 7  # close=106 first closes above the 105 swing high


def test_choch_on_reversal_against_established_trend():
    candles = make_candles(OHLC)
    swings = detect_swings(candles, swing_length=2)
    events = detect_structure_events(candles, swings)

    choch = next(e for e in events if e.event_type == StructureEventType.CHOCH)
    assert choch.direction == Direction.BEARISH
    assert choch.broken_price == 98


def test_promote_confirmed_shifts_requires_same_direction_bos_after_choch():
    candles = make_candles(OHLC)
    swings = detect_swings(candles, swing_length=2)
    events = detect_structure_events(candles, swings)

    # No confirming bearish BOS follows the CHoCH in this sample, so no MSS yet.
    assert promote_confirmed_shifts(events) == []
