import pytest

from app.replay.engine import ReplayEngine, ReplayError
from app.smc.engine import SMCConfig
from tests.smc.conftest import make_candles
from tests.smc.test_swings import OHLC


def test_look_ahead_prevention_matches_truncated_offline_analysis():
    """The whole point of replay: analysis at step N must be identical to
    running the SMC engine directly on candles[:N+1] — never on more."""
    candles = make_candles(OHLC)
    engine = ReplayEngine(candles, smc_config=SMCConfig(swing_length=2))

    engine.advance(steps=7)  # cursor now at index 7
    replay_context, _ = engine.analyze()

    from app.smc.engine import SMCEngine

    offline_context = SMCEngine(SMCConfig(swing_length=2)).analyze(candles[:8])

    assert [e.event_type for e in replay_context.structure_events] == [
        e.event_type for e in offline_context.structure_events
    ]
    assert len(replay_context.swings) == len(offline_context.swings)


def test_cannot_see_future_events_before_they_happen():
    candles = make_candles(OHLC)
    engine = ReplayEngine(candles, smc_config=SMCConfig(swing_length=2))
    engine.advance(steps=7)  # right after the bullish BOS, before the later CHoCH
    context, _ = engine.analyze()

    event_types = {e.event_type.value for e in context.structure_events}
    assert "CHOCH" not in event_types  # that only happens later in the series


def test_buy_set_stop_target_and_auto_close_on_target_hit():
    ohlc = [
        (100, 100, 99, 100),
        (100, 101, 99, 100),
        (100, 108, 100, 107),  # target hit intrabar
    ]
    candles = make_candles(ohlc)
    engine = ReplayEngine(candles, starting_balance=10_000)

    engine.buy(quantity=10)
    engine.set_stop(95)
    engine.set_target(105)

    engine.advance(steps=2)

    assert engine.open_trade is None
    assert len(engine.closed_trades) == 1
    trade = engine.closed_trades[0]
    assert trade.exit_price == 105
    assert trade.pnl == pytest.approx((105 - 100) * 10)
    assert engine.balance == pytest.approx(10_000 + 50)


def test_cannot_open_second_position_while_one_is_open():
    candles = make_candles(OHLC)
    engine = ReplayEngine(candles)
    engine.buy(10)
    with pytest.raises(ReplayError):
        engine.buy(5)


def test_statistics_after_a_losing_and_a_winning_trade():
    ohlc = [
        (100, 101, 95, 100),
        (100, 101, 90, 91),  # stop hit for a long
        (91, 92, 89, 91),
        (91, 100, 90, 99),  # target hit for a second long
    ]
    candles = make_candles(ohlc)
    engine = ReplayEngine(candles, starting_balance=10_000)

    engine.buy(1)
    engine.set_stop(92)
    engine.advance(steps=1)  # stop hit -> loss

    engine.buy(1)
    engine.set_target(98)
    engine.advance(steps=2)  # target hit -> win

    stats = engine.statistics
    assert stats.trades == 2
    assert stats.win_rate == 0.5
    assert stats.best_trade > 0
    assert stats.worst_trade < 0
