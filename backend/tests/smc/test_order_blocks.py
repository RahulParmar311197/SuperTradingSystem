from app.smc.fvg import detect_fvgs
from app.smc.order_blocks import detect_order_blocks
from app.smc.structure import detect_structure_events
from app.smc.swings import detect_swings
from app.smc.types import Direction
from tests.smc.conftest import make_candles
from tests.smc.test_swings import OHLC


def test_bullish_order_block_is_last_bearish_candle_before_breakout():
    candles = make_candles(OHLC)
    swings = detect_swings(candles, swing_length=2)
    events = detect_structure_events(candles, swings)
    fvgs = detect_fvgs(candles)

    blocks = detect_order_blocks(candles, events, fvgs)
    bullish_blocks = [b for b in blocks if b.direction == Direction.BULLISH]

    assert bullish_blocks
    block = bullish_blocks[0]
    origin = candles[block.created_index]
    assert origin.close < origin.open  # a bearish candle
    assert 0.0 <= block.strength <= 1.0
