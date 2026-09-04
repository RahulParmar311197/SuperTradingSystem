import pytest

from app.database.models.strategy import Direction
from app.options.greeks import OptionType
from app.options.payoff import OptionLeg, compute_payoff_summary, total_payoff
from app.options.strategies import bear_call_spread, bull_call_spread, iron_condor, long_call

CHAIN = {
    24800: {"CALL": 250.0, "PUT": 60.0},
    24900: {"CALL": 180.0, "PUT": 90.0},
    25000: {"CALL": 120.0, "PUT": 120.0},
    25100: {"CALL": 80.0, "PUT": 170.0},
    25200: {"CALL": 50.0, "PUT": 230.0},
}


def test_long_call_max_loss_is_premium_paid():
    legs = long_call(CHAIN, strike=25000, quantity=50, lot_size=1)
    summary = compute_payoff_summary(legs)

    assert summary.max_loss == pytest.approx(-120.0 * 50)
    assert summary.max_profit is None  # unlimited upside
    assert summary.net_premium == pytest.approx(120.0 * 50)


def test_bull_call_spread_has_capped_profit_and_loss():
    legs = bull_call_spread(CHAIN, long_strike=25000, short_strike=25200, quantity=50, lot_size=1)
    summary = compute_payoff_summary(legs)

    net_debit = (120.0 - 50.0) * 50
    max_profit_expected = (25200 - 25000) * 50 - net_debit

    assert summary.net_premium == pytest.approx(net_debit)
    assert summary.max_profit == pytest.approx(max_profit_expected, rel=1e-3)
    assert summary.max_loss == pytest.approx(-net_debit, rel=1e-3)
    assert len(summary.breakevens) == 1


def test_bear_call_spread_is_a_credit_with_bounded_loss():
    legs = bear_call_spread(CHAIN, short_strike=25000, long_strike=25200, quantity=50, lot_size=1)
    summary = compute_payoff_summary(legs)

    net_credit = (120.0 - 50.0) * 50
    assert summary.net_premium == pytest.approx(-net_credit)
    assert summary.max_profit == pytest.approx(net_credit, rel=1e-3)
    max_loss_expected = -((25200 - 25000) * 50 - net_credit)
    assert summary.max_loss == pytest.approx(max_loss_expected, rel=1e-3)


def test_iron_condor_has_two_breakevens_and_bounded_risk():
    legs = iron_condor(
        CHAIN, put_long_strike=24800, put_short_strike=24900, call_short_strike=25100, call_long_strike=25200, quantity=50
    )
    summary = compute_payoff_summary(legs)

    assert summary.max_profit is not None
    assert summary.max_loss is not None
    assert len(summary.breakevens) == 2


def test_multi_leg_quantity_scales_payoff_correctly():
    single = [OptionLeg(OptionType.CALL, strike=25000, premium=120.0, quantity=1, direction=Direction.LONG, lot_size=50)]
    double = [OptionLeg(OptionType.CALL, strike=25000, premium=120.0, quantity=2, direction=Direction.LONG, lot_size=50)]

    price = 25200.0
    assert total_payoff(double, price) == pytest.approx(2 * total_payoff(single, price))
