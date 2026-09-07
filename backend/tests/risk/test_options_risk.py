import pytest

from app.database.models.strategy import Direction
from app.options.greeks import OptionType
from app.options.payoff import OptionLeg, compute_payoff_summary
from app.options.strategies import bull_call_spread, long_call
from app.risk.kill_switch import KillSwitchState
from app.risk.limits import RiskDecision, RiskLimits
from app.risk.options_risk import OptionsRiskProposal, evaluate_options_risk

CHAIN = {
    24800: {"CALL": 250.0, "PUT": 60.0},
    25000: {"CALL": 120.0, "PUT": 120.0},
    25200: {"CALL": 50.0, "PUT": 230.0},
}


def _base_proposal(**overrides) -> OptionsRiskProposal:
    legs = bull_call_spread(CHAIN, long_strike=25000, short_strike=25200, quantity=50, lot_size=1)
    payoff = compute_payoff_summary(legs)
    defaults = dict(
        account_id="acct-1",
        account_balance=100_000.0,
        current_exposure=0.0,
        payoff=payoff,
        broker_healthy=True,
    )
    defaults.update(overrides)
    return OptionsRiskProposal(**defaults)


def test_approves_a_defined_risk_spread_within_limits():
    result = evaluate_options_risk(_base_proposal())
    assert result.decision == RiskDecision.APPROVE
    assert result.failed_checks == []


def test_rejects_when_global_kill_switch_active():
    kill_switch = KillSwitchState()
    kill_switch.kill_global()
    result = evaluate_options_risk(_base_proposal(), kill_switch=kill_switch)
    assert result.decision == RiskDecision.REJECT
    assert "Global kill switch" in result.reason


def test_rejects_when_max_loss_exceeds_exposure_limit():
    limits = RiskLimits(max_exposure_pct=1.0)  # 1% of 100k = 1000; spread's max_loss is 3500
    result = evaluate_options_risk(_base_proposal(), limits=limits)
    assert result.decision == RiskDecision.REJECT
    assert any(c.name == "exposure_limit" and not c.passed for c in result.checks)


def test_rejects_when_liquidity_unacceptable():
    result = evaluate_options_risk(_base_proposal(liquidity_acceptable=False))
    assert result.decision == RiskDecision.REJECT
    assert any(c.name == "liquidity_acceptable" and not c.passed for c in result.checks)


def test_rejects_when_broker_unhealthy():
    result = evaluate_options_risk(_base_proposal(broker_healthy=False))
    assert result.decision == RiskDecision.REJECT


def test_premium_deviation_defaults_to_a_no_op():
    # premium_deviation_pct defaults to 0.0 -- a clean strategy must still
    # approve even though max_premium_deviation_pct exists.
    result = evaluate_options_risk(_base_proposal())
    assert any(c.name == "premium_matches_market" and c.passed for c in result.checks)


def test_rejects_when_premium_deviates_from_the_real_market_quote():
    # Regression test: a client-supplied leg `premium` is otherwise
    # trusted input that sizes this strategy's own payoff/risk math
    # (compute_payoff_summary), unchecked against anything real -- the
    # same shape of gap already fixed for POST /orders's `entry` field
    # (RiskEngine's entry_matches_market), reopened here since that fix
    # never touched options execution.
    proposal = _base_proposal(premium_deviation_pct=10.0)  # default limit is 5.0%
    result = evaluate_options_risk(proposal)
    assert result.decision == RiskDecision.REJECT
    assert any(c.name == "premium_matches_market" and not c.passed for c in result.checks)


def test_unbounded_risk_strategy_uses_capital_requirement_not_zero():
    """A naked long call has unbounded upside but a *bounded, non-zero*
    max_loss (the premium paid) — this asserts the risk gate reads a real
    number for it, not a None/zero that would look risk-free."""
    legs = long_call(CHAIN, strike=25000, quantity=50, lot_size=1)
    payoff = compute_payoff_summary(legs)
    assert payoff.max_profit is None  # unlimited upside, sanity check on the fixture

    limits = RiskLimits(max_exposure_pct=1.0)  # premium paid (120*50=6000) exceeds 1% of 100k
    result = evaluate_options_risk(_base_proposal(payoff=payoff), limits=limits)
    assert result.decision == RiskDecision.REJECT
    assert any(c.name == "exposure_limit" and not c.passed for c in result.checks)


def _synthetic_short(call_premium: float) -> list[OptionLeg]:
    """LONG 25000 put + SHORT 26000 call: loss is unbounded above 26000, so
    `compute_payoff_summary` reports `max_loss=None`. `call_premium` alone
    decides whether entering it is a net debit or a net credit; it does not
    change the risk shape at all."""
    return [
        OptionLeg(OptionType.PUT, 25000, 50.0, 1, Direction.LONG, lot_size=50),
        OptionLeg(OptionType.CALL, 26000, call_premium, 1, Direction.SHORT, lot_size=50),
    ]


def test_unbounded_loss_is_sized_by_the_curve_not_by_the_entry_debit():
    # Regression test: `evaluate_options_risk` sized an unbounded-risk
    # combination from `PayoffResult.capital_requirement`, which
    # `compute_payoff_summary` sets to the net *debit* whenever there is
    # one, falling through to the worst sampled loss only for a net credit.
    # A synthetic short entered for a 1,000 debit was therefore checked as
    # 1% of a 100,000 account while its payoff curve was already 651,000
    # underwater inside the sampled range -- and `POST /options/execute`
    # builds legs straight from the client payload, so this shape is
    # reachable by anyone with LIVE_TRADE.
    payoff = compute_payoff_summary(_synthetic_short(call_premium=30.0))
    assert payoff.max_loss is None, "fixture must be unbounded on the loss side"
    assert payoff.net_premium > 0, "fixture must be a net debit -- that is the broken branch"

    result = evaluate_options_risk(_base_proposal(payoff=payoff), limits=RiskLimits(max_exposure_pct=100.0))
    assert result.decision == RiskDecision.REJECT
    exposure = next(c for c in result.checks if c.name == "exposure_limit")
    assert not exposure.passed
    # The value is the point: the check must see the real six-figure
    # downside, not the 1.00% the 1,000 debit produced.
    assert float(exposure.detail.split()[2].rstrip("%")) > 500


def test_premium_sign_does_not_change_the_risk_of_the_same_position():
    # The sharpest form of the same contract. These two positions have
    # identical unbounded downside; they differ only in one leg's premium,
    # which flips the entry between a 1,000 debit and a 500 credit. Sizing
    # off `capital_requirement` made that sign decide the outcome: the
    # credit version was rejected at 649.50% and the debit version approved
    # at 1.00%.
    limits = RiskLimits(max_exposure_pct=100.0)
    exposures = []
    for call_premium in (30.0, 60.0):
        payoff = compute_payoff_summary(_synthetic_short(call_premium))
        assert payoff.max_loss is None
        result = evaluate_options_risk(_base_proposal(payoff=payoff), limits=limits)
        assert result.decision == RiskDecision.REJECT
        detail = next(c for c in result.checks if c.name == "exposure_limit").detail
        exposures.append(float(detail.split()[2].rstrip("%")))

    debit_exposure, credit_exposure = exposures
    # Same position, so the two must agree to within the premium difference
    # itself (1,500 on a 100,000 account = 1.5 percentage points), rather
    # than differing by the ~648 points the old sizing produced.
    assert abs(debit_exposure - credit_exposure) < 2.0


def test_bounded_strategies_are_unaffected_by_the_unbounded_loss_path():
    # `max_loss is not None` must still win outright: a defined-risk spread
    # is sized by its real max_loss (3500 here), never by the worst value
    # sampled beyond it.
    payoff = compute_payoff_summary(
        bull_call_spread(CHAIN, long_strike=25000, short_strike=25200, quantity=50, lot_size=1)
    )
    assert payoff.max_loss is not None
    result = evaluate_options_risk(_base_proposal(payoff=payoff), limits=RiskLimits(max_exposure_pct=4.0))
    exposure = next(c for c in result.checks if c.name == "exposure_limit")
    assert exposure.passed
    assert float(exposure.detail.split()[2].rstrip("%")) == pytest.approx(3.5, rel=1e-6)
