from app.risk.engine import RiskEngine, TradeRiskProposal, calculate_position_size
from app.risk.kill_switch import KillSwitchState
from app.risk.limits import RiskDecision, RiskLimits


def _base_proposal(**overrides) -> TradeRiskProposal:
    defaults = dict(
        account_id="acct-1",
        strategy_id="strat-1",
        entry=100.0,
        stop=98.0,
        account_balance=100_000.0,
        open_positions=1,
        trades_today=2,
        daily_pnl=0.0,
        weekly_pnl=0.0,
        current_exposure=0.0,
        strategy_allocation=0.0,
        market_data_age_seconds=1.0,
        broker_healthy=True,
    )
    defaults.update(overrides)
    return TradeRiskProposal(**defaults)


def test_position_sizing_respects_risk_percent():
    qty = calculate_position_size(account_balance=100_000, risk_percent=0.5, entry=100, stop=98)
    # risk_amount = 500, risk_per_unit = 2 -> 250 units
    assert qty == 250.0


def test_position_sizing_caps_at_max_position_size():
    qty = calculate_position_size(
        account_balance=100_000, risk_percent=0.5, entry=100, stop=98, max_position_size=50
    )
    assert qty == 50.0


def test_approves_a_clean_trade():
    result = RiskEngine().evaluate(_base_proposal())
    assert result.decision == RiskDecision.APPROVE
    assert result.failed_checks == []


def test_rejects_when_global_kill_switch_active():
    kill_switch = KillSwitchState()
    kill_switch.kill_global()
    result = RiskEngine(kill_switch=kill_switch).evaluate(_base_proposal())
    assert result.decision == RiskDecision.REJECT
    assert "Global kill switch" in result.reason


def test_rejects_when_strategy_killed():
    kill_switch = KillSwitchState()
    kill_switch.kill_strategy("strat-1")
    result = RiskEngine(kill_switch=kill_switch).evaluate(_base_proposal())
    assert result.decision == RiskDecision.REJECT


def test_rejects_when_daily_loss_limit_exceeded():
    limits = RiskLimits(max_daily_loss_pct=2.0)
    proposal = _base_proposal(daily_pnl=-3000.0)  # -3% of 100k
    result = RiskEngine(limits=limits).evaluate(proposal)
    assert result.decision == RiskDecision.REJECT
    assert any(c.name == "daily_loss_limit" and not c.passed for c in result.checks)


def test_rejects_when_max_open_positions_reached():
    limits = RiskLimits(max_open_positions=1)
    proposal = _base_proposal(open_positions=1)
    result = RiskEngine(limits=limits).evaluate(proposal)
    assert result.decision == RiskDecision.REJECT


def test_rejects_when_market_data_stale():
    limits = RiskLimits(market_data_max_staleness_seconds=5.0)
    proposal = _base_proposal(market_data_age_seconds=30.0)
    result = RiskEngine(limits=limits).evaluate(proposal)
    assert result.decision == RiskDecision.REJECT


def test_correlated_exposure_defaults_to_a_no_op():
    # correlated_exposure defaults to 0.0 — a clean trade must still
    # approve even though max_correlated_exposure_pct exists.
    result = RiskEngine().evaluate(_base_proposal())
    assert any(c.name == "correlated_exposure_limit" and c.passed for c in result.checks)


def test_rejects_when_correlated_exposure_limit_exceeded():
    limits = RiskLimits(max_correlated_exposure_pct=10.0)
    proposal = _base_proposal(correlated_exposure=50_000.0)  # 50% of 100k balance
    result = RiskEngine(limits=limits).evaluate(proposal)
    assert result.decision == RiskDecision.REJECT
    assert any(c.name == "correlated_exposure_limit" and not c.passed for c in result.checks)


def test_rejects_when_broker_unhealthy():
    result = RiskEngine().evaluate(_base_proposal(broker_healthy=False))
    assert result.decision == RiskDecision.REJECT


def test_rejects_when_exposure_limit_exceeded():
    limits = RiskLimits(max_exposure_pct=1.0)
    proposal = _base_proposal(current_exposure=50_000.0)
    result = RiskEngine(limits=limits).evaluate(proposal)
    assert result.decision == RiskDecision.REJECT
