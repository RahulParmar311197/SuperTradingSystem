"""An account with no money had no risk limits at all.

Every percentage gate in both risk evaluators divides BY the account
balance, and each one fell back to a PASSING value when that balance was
falsy: the loss gates to 0%, the exposure gates to a flat 100.0 -- which
clears a `max_*_pct` of 100, the default for `max_exposure_pct`,
`max_strategy_allocation_pct` and `max_correlated_exposure_pct`.

Measured on one equity proposal: 100,000,000 of fresh notional against a
50,000 daily loss and 10,000,000 of exposure already open, with the
stock `RiskLimits` (daily 2%, weekly 5%, exposure 100%):

    balance  100,000.00 -> refused, 5 checks failed
    balance        0.00 -> APPROVED, 0 checks failed
    balance   -5,000.00 -> APPROVED, 0 checks failed

Not a hypothetical state. `UpstoxBroker.get_account` returns the broker's
real figure, so an account funded to zero -- or one whose balance an
adapter could not parse -- reports exactly this, and every capital
control switches off at the moment the account can least afford it.

TWO EVALUATORS, TWO FIXES. `app/risk/engine.py` gates equity orders
(POST /orders, PaperTradingEngine, AutoTradeSupervisor) and
`app/risk/options_risk.py` gates POST /options/execute. They take
different proposals and share no code, so neither guard stands in for the
other -- with only the equity engine fixed, an options entry still went
through on a blown account, which is how
`tests/api/test_options_execute.py` caught the omission.

THE SHAPE OF THE FIX. One `account_funded` check names the cause, and the
checks that cannot be computed are SKIPPED rather than recorded with a
fabricated number -- the rule rounds 121 and 134 settled for
`liquidity_acceptable` and the options gates, so a `RiskEvent` audit row
lists only what really governed the decision. The checks that need no
balance (`max_open_positions`, `max_trades_per_day`, the kill switch,
market-data freshness, broker health) still run.

ENTRY-ONLY, deliberately. An exit takes on no risk, and refusing one
would strand a user in a position exactly when their account is emptiest
-- the same reasoning behind the reducing-order exemption these checks
already sit inside.
"""

import pytest

from app.options.payoff import compute_payoff_summary
from app.options.strategies import bull_call_spread
from app.risk.engine import RiskEngine, TradeRiskProposal
from app.risk.limits import RiskDecision, RiskLimits
from app.risk.options_risk import OptionsRiskProposal, evaluate_options_risk

CHAIN = {
    24800: {"CALL": 250.0, "PUT": 60.0},
    25000: {"CALL": 120.0, "PUT": 120.0},
    25200: {"CALL": 50.0, "PUT": 230.0},
}

# The five equity gates that divide by the balance, and the three options
# ones. Named rather than counted so a gate added later without a
# denominator guard shows up as a missing name, not a changed number.
EQUITY_PERCENTAGE_CHECKS = {
    "daily_loss_limit",
    "weekly_loss_limit",
    "exposure_limit",
    "strategy_allocation_limit",
    "correlated_exposure_limit",
}
OPTIONS_PERCENTAGE_CHECKS = {"exposure_limit", "daily_loss_limit", "weekly_loss_limit"}


def _equity(balance: float, **overrides) -> TradeRiskProposal:
    """A proposal that every percentage gate should refuse outright: a
    100,000,000 notional entry on an account already 50,000 down for the
    day with 10,000,000 open."""
    defaults = dict(
        account_id="acct-1",
        strategy_id=None,
        entry=100.0,
        stop=95.0,
        account_balance=balance,
        open_positions=0,
        trades_today=0,
        daily_pnl=-50_000.0,
        weekly_pnl=-50_000.0,
        current_exposure=10_000_000.0,
        strategy_allocation=10_000_000.0,
        market_data_age_seconds=0.0,
        broker_healthy=True,
        proposed_quantity=1_000_000.0,
    )
    defaults.update(overrides)
    return TradeRiskProposal(**defaults)


def _options(balance: float, **overrides) -> OptionsRiskProposal:
    legs = bull_call_spread(CHAIN, long_strike=25000, short_strike=25200, quantity=50, lot_size=1)
    defaults = dict(
        account_id="acct-1",
        account_balance=balance,
        current_exposure=10_000_000.0,
        payoff=compute_payoff_summary(legs),
        broker_healthy=True,
        daily_pnl=-50_000.0,
        weekly_pnl=-50_000.0,
    )
    defaults.update(overrides)
    return OptionsRiskProposal(**defaults)


def _names(result, *, failed_only: bool = False) -> set[str]:
    return {c.name for c in result.checks if not (failed_only and c.passed)}


# --- the finding ----------------------------------------------------------


@pytest.mark.parametrize("balance", [0.0, -5_000.0])
def test_an_equity_entry_is_refused_when_the_account_has_no_money(balance):
    """The headline. This exact proposal is refused by five gates at
    100,000 and was APPROVED at 0.00."""
    result = RiskEngine(limits=RiskLimits()).evaluate(_equity(balance))

    assert result.decision == RiskDecision.REJECT, [c.name for c in result.checks]
    assert "account_funded" in _names(result, failed_only=True)


@pytest.mark.parametrize("balance", [0.0, -5_000.0])
def test_an_options_entry_is_refused_when_the_account_has_no_money(balance):
    """The second evaluator. Fixing only `app/risk/engine.py` leaves this
    one approving, which is what happened first."""
    result = evaluate_options_risk(_options(balance))

    assert result.decision == RiskDecision.REJECT, [c.name for c in result.checks]
    assert "account_funded" in _names(result, failed_only=True)


# --- non-vacuity: the same proposal, funded, is refused for real reasons --


def test_the_funded_proposal_is_refused_by_the_percentage_gates_themselves():
    """Without this the headline would pass on a proposal that nothing
    objects to, proving only that `account_funded` fires. Every one of the
    five real gates must reject this trade at 100,000."""
    result = RiskEngine(limits=RiskLimits()).evaluate(_equity(100_000.0))

    assert result.decision == RiskDecision.REJECT
    assert EQUITY_PERCENTAGE_CHECKS <= _names(result, failed_only=True)
    assert "account_funded" not in _names(result)


def test_the_funded_options_proposal_is_refused_by_its_own_gates():
    result = evaluate_options_risk(_options(100_000.0))

    assert result.decision == RiskDecision.REJECT
    assert OPTIONS_PERCENTAGE_CHECKS <= _names(result, failed_only=True)
    assert "account_funded" not in _names(result)


# --- what the audit row says ----------------------------------------------


def test_the_gates_that_cannot_be_computed_are_skipped_not_fabricated():
    """Rounds 121 and 134's rule: "we could not check" is not "we checked
    and it failed" any more than it is "we checked and it is fine". A
    percentage of nothing is not a number, so those checks must be absent
    from the audit row -- not present carrying an invented figure."""
    result = RiskEngine(limits=RiskLimits()).evaluate(_equity(0.0))

    assert not (EQUITY_PERCENTAGE_CHECKS & _names(result)), (
        f"a check with no denominator was recorded anyway: "
        f"{EQUITY_PERCENTAGE_CHECKS & _names(result)}"
    )


def test_the_checks_that_need_no_balance_still_run():
    """The over-fix guard in the other direction: skipping the whole
    entry-only block would also drop `max_open_positions` and
    `max_trades_per_day`, which need no denominator and are exactly the
    caps an emptied account is most likely to be hitting."""
    result = RiskEngine(limits=RiskLimits()).evaluate(
        _equity(0.0, open_positions=99, trades_today=99)
    )

    names = _names(result)
    assert "max_open_positions" in names
    assert "max_trades_per_day" in names
    failed = _names(result, failed_only=True)
    assert {"max_open_positions", "max_trades_per_day"} <= failed, (
        "both caps are breached by this proposal and must be recorded as failed"
    )


# --- the exemption the fix must not break ---------------------------------


@pytest.mark.parametrize("balance", [0.0, -5_000.0])
def test_an_exit_is_still_allowed_on_an_account_with_no_money(balance):
    """The risk the guard introduces rather than the one it removes. An
    order that only reduces takes on no risk; refusing it would leave a
    user unable to get out of a position at the worst possible moment.
    `account_funded` is entry-only for the same reason the gates it sits
    beside are."""
    result = RiskEngine(limits=RiskLimits()).evaluate(_equity(balance, is_reducing=True))

    assert result.decision == RiskDecision.APPROVE, [c.name for c in result.checks if not c.passed]
    assert "account_funded" not in _names(result)


@pytest.mark.parametrize("balance", [0.0, -5_000.0])
def test_an_options_exit_is_still_allowed_on_an_account_with_no_money(balance):
    result = evaluate_options_risk(_options(balance, is_reducing=True))

    assert result.decision == RiskDecision.APPROVE, [c.name for c in result.checks if not c.passed]
    assert "account_funded" not in _names(result)


# --- the boundary ---------------------------------------------------------


def test_a_tiny_but_positive_balance_is_gated_arithmetically_not_by_the_guard():
    """The guard fires at <= 0 and nowhere else. A balance of one rupee is
    a real denominator: the gates compute enormous percentages and refuse
    on their own terms, which is a different (and more informative)
    refusal than "unfunded". An over-fix that tripped on "too small"
    passes the headline and fails here."""
    result = RiskEngine(limits=RiskLimits()).evaluate(_equity(1.0))

    assert result.decision == RiskDecision.REJECT
    assert "account_funded" not in _names(result)
    assert EQUITY_PERCENTAGE_CHECKS <= _names(result, failed_only=True)
