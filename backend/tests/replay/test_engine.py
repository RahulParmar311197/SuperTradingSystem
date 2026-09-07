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


# Entry 100 with an initial stop at 90 is 10/unit of risk, so every exit
# below is a fixed, independently-known multiple of R regardless of where
# the stop is later moved to.
MANAGED_TRADE_OHLC = [
    (100, 100, 99, 100),
    (100, 115, 100, 114),  # runs up; this is where the user manages the stop
    (114, 131, 113, 130),  # target hit (and any trailed stop below 113 is not)
]


def test_r_multiple_is_measured_against_the_stop_as_first_placed():
    # Regression test: `close` computed `r_multiple` from `trade.stop`,
    # which MOVE SL (blueprint §43, exposed as the `set_stop` action on
    # POST /replay/{id}/action) overwrites. R is reward in units of the
    # risk actually taken, and that is fixed when the stop is first
    # placed -- measuring against the current stop makes R mean something
    # different for every trade and corrupts blueprint §44's "Average R".
    engine = ReplayEngine(make_candles(MANAGED_TRADE_OHLC), starting_balance=10_000)
    engine.buy(quantity=1)
    engine.set_stop(90)  # risk = 10/unit
    engine.set_target(130)
    engine.advance(steps=1)
    engine.move_stop(120)  # trail it up; candle 3's low of 113 takes it out
    engine.advance(steps=1)

    trade = engine.closed_trades[0]
    assert trade.exit_price == 120
    assert trade.pnl == pytest.approx(20.0)
    # 20 of profit against 10 of risk taken is 2R. Against the trailed
    # stop it read 1.0.
    assert trade.r_multiple == pytest.approx(2.0)
    assert engine.statistics.average_r == pytest.approx(2.0)


def test_a_trade_managed_to_breakeven_still_counts_toward_average_r():
    # The sharper half: moving the stop to entry is the most common
    # management action there is, and it left `stop == entry_price`, so
    # the `entry_price != stop` guard skipped the trade and a 3R winner
    # contributed *nothing* to Average R -- the statistic silently
    # excluded exactly the trades a user had managed well.
    engine = ReplayEngine(make_candles(MANAGED_TRADE_OHLC), starting_balance=10_000)
    engine.buy(quantity=1)
    engine.set_stop(90)  # risk = 10/unit
    engine.set_target(130)
    engine.advance(steps=1)
    engine.move_stop(100)  # breakeven
    engine.advance(steps=1)

    trade = engine.closed_trades[0]
    assert trade.exit_price == 130
    assert trade.pnl == pytest.approx(30.0)
    assert trade.r_multiple == pytest.approx(3.0)
    assert engine.statistics.average_r == pytest.approx(3.0)


def test_r_multiple_is_unchanged_when_the_stop_is_never_moved():
    # The unmanaged case must be untouched: with one stop ever placed,
    # `initial_stop` and `stop` are the same number.
    engine = ReplayEngine(make_candles(MANAGED_TRADE_OHLC), starting_balance=10_000)
    engine.buy(quantity=1)
    engine.set_stop(90)
    engine.set_target(130)
    engine.advance(steps=2)

    trade = engine.closed_trades[0]
    assert trade.exit_price == 130
    assert trade.r_multiple == pytest.approx(3.0)
