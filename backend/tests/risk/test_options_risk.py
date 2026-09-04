from app.options.payoff import compute_payoff_summary
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
