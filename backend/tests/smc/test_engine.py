from app.smc.engine import SMCConfig, SMCEngine
from tests.smc.conftest import make_candles
from tests.smc.test_swings import OHLC


def test_engine_produces_full_context():
    candles = make_candles(OHLC)
    engine = SMCEngine(SMCConfig(swing_length=2))
    context = engine.analyze(candles)

    assert context.swings
    assert context.structure_events
    assert context.bias == "BEARISH"  # last event in the sample is the bearish CHoCH
    assert context.dealing_range is not None
    assert context.current_zone in {"PREMIUM", "DISCOUNT", "EQUILIBRIUM"}


def test_engine_is_look_ahead_safe_when_given_truncated_history():
    full = make_candles(OHLC)
    engine = SMCEngine(SMCConfig(swing_length=2))

    truncated_context = engine.analyze(full[:8])  # cuts off right after the BOS at idx7
    full_context = engine.analyze(full)

    # The bearish CHoCH only exists once later candles are visible.
    truncated_types = {e.event_type for e in truncated_context.structure_events}
    full_types = {e.event_type for e in full_context.structure_events}
    assert "CHOCH" not in {t.value for t in truncated_types}
    assert "CHOCH" in {t.value for t in full_types}
