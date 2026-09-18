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


def test_an_unquoted_strategy_still_approves_but_claims_no_premium_check():
    """The intent of this test is unchanged: a clean strategy with nothing
    to compare a premium against must still approve, because
    `max_premium_deviation_pct` existing must not make the endpoint
    unusable.

    What changed is the *claim*. `premium_deviation_pct` used to default to
    0.0 -- "checked, and exactly on the market" -- so a strategy nothing
    had quoted recorded `premium_matches_market: true` in its RiskEvent.
    It now defaults to `None`, the check is skipped, and the audit row
    lists only what actually governed the decision.
    """
    result = evaluate_options_risk(_base_proposal())
    assert result.decision == RiskDecision.APPROVE
    assert "premium_matches_market" not in {c.name for c in result.checks}


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


# --- "not assessed" is not "assessed and fine" -----------------------------
#
# The same defect an earlier round fixed for the equity sibling
# (`TradeRiskProposal.liquidity_acceptable`), found still present here. That
# round's note claimed this side was safe because it "is genuinely computed
# by app/api/options.py from OptionSnapshot data" -- the computation exists,
# but NO production code writes `option_snapshots`, `option_contracts` or
# `option_chains`, so the branch performing it never runs outside the test
# suite, and every real options RiskEvent took the everything-is-fine
# defaults.

_UNEVALUATED = ("liquidity_acceptable", "premium_matches_market", "market_data_fresh")


def test_an_unquoted_strategy_records_none_of_the_three_quote_checks():
    """Behavioural proof, and the measurement that opened this round.

    Before: all three appeared in the audit row as passed. An operator
    reading `GET /admin/risk-events` could not tell a strategy whose quotes
    were checked and were fine from one where the gate was never wired up.
    """
    result = evaluate_options_risk(_base_proposal())
    recorded = {c.name for c in result.checks}
    assert not (recorded & set(_UNEVALUATED)), sorted(recorded & set(_UNEVALUATED))
    # ... and it still approves: skipping is not rejecting.
    assert result.decision == RiskDecision.APPROVE


def test_a_quoted_strategy_records_all_three():
    """Control. Skipping must happen only when there was nothing to check.

    If this ever fails, the fix above has not made the checks conditional
    on evidence -- it has switched them off.
    """
    result = evaluate_options_risk(
        _base_proposal(
            liquidity_acceptable=True,
            premium_deviation_pct=0.5,
            market_data_age_seconds=2.0,
        )
    )
    recorded = {c.name for c in result.checks}
    assert set(_UNEVALUATED) <= recorded, sorted(set(_UNEVALUATED) - recorded)
    assert result.decision == RiskDecision.APPROVE


def test_each_of_the_three_still_rejects_on_real_evidence():
    """Control. Making the checks skippable must not make them toothless:
    a real bad value still has to fail, one gate at a time.
    """
    illiquid = evaluate_options_risk(_base_proposal(liquidity_acceptable=False))
    assert illiquid.decision == RiskDecision.REJECT
    assert any(c.name == "liquidity_acceptable" and not c.passed for c in illiquid.checks)

    off_market = evaluate_options_risk(_base_proposal(premium_deviation_pct=10.0))
    assert off_market.decision == RiskDecision.REJECT
    assert any(c.name == "premium_matches_market" and not c.passed for c in off_market.checks)

    stale = evaluate_options_risk(_base_proposal(market_data_age_seconds=3600.0))
    assert stale.decision == RiskDecision.REJECT
    assert any(c.name == "market_data_fresh" and not c.passed for c in stale.checks)


def test_zero_staleness_is_still_a_real_claim_and_is_recorded():
    """Control on the distinction itself. `0.0` means "this quote is from
    this instant" -- a legitimate, checkable assertion a caller may make --
    and must be recorded as a passed check. Only `None` skips. If `0.0`
    started skipping too, the fix would have thrown away the honest case
    along with the fabricated one.
    """
    result = evaluate_options_risk(_base_proposal(market_data_age_seconds=0.0))
    assert any(c.name == "market_data_fresh" and c.passed for c in result.checks)
