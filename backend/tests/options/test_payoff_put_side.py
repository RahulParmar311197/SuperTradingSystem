"""The sampled payoff window has to reach the put side's real extreme.

`compute_payoff_summary` infers boundedness from whether the curve is
still sloping at a sampled edge. That inference is only sound where the
edge is a truncation. An underlying cannot trade below zero, so price 0 is
a hard domain boundary and the put side of any combination reaches its
true extreme there -- a short put's maximum loss, a long put's maximum
profit -- both finite and computable.

The default range used to start at `min(strikes) * 0.5`, which stops short
of that extreme while the curve is still falling. Everything downstream
then read "unbounded" for outcomes that are in fact bounded, and
`worst_sampled_loss` -- the figure `app.risk.options_risk` falls back to
for an unbounded combination -- reported roughly half the real number.

Why the existing tests could not see this. Every fixture in
`tests/options/test_payoff.py` is call-side or fully defined-risk
(`long_call`, `bull_call_spread`, `bear_call_spread`, `iron_condor`); not
one carries an uncovered short put. The "unbounded" fixture in
`tests/risk/test_options_risk.py` is `_synthetic_short` (long put + short
call), whose unboundedness comes from the *call* leg running away to the
right -- the put-side truncation is never what any assertion turns on.
Fake-coverage shape (b): a fixture that structurally cannot reach the
breaking state.
"""

import pytest

from app.database.models.strategy import Direction
from app.options.greeks import OptionType
from app.options.payoff import OptionLeg, compute_payoff_summary, total_payoff
from app.risk.options_risk import OptionsRiskProposal, evaluate_options_risk

# 1000-strike put at 100, one lot of 150 -- a cash-secured put, the plainest
# short-volatility position there is.
STRIKE, PREMIUM, LOT = 1000.0, 100.0, 150


def _short_put() -> list[OptionLeg]:
    return [OptionLeg(OptionType.PUT, STRIKE, PREMIUM, 1, Direction.SHORT, LOT)]


def _long_put() -> list[OptionLeg]:
    return [OptionLeg(OptionType.PUT, STRIKE, PREMIUM, 1, Direction.LONG, LOT)]


def _short_straddle() -> list[OptionLeg]:
    return [
        OptionLeg(OptionType.CALL, STRIKE, PREMIUM, 1, Direction.SHORT, LOT),
        OptionLeg(OptionType.PUT, STRIKE, PREMIUM, 1, Direction.SHORT, LOT),
    ]


def test_a_naked_short_puts_max_loss_is_bounded_and_exact():
    # Regression test: this reported None. The loss is not unbounded -- it
    # is exactly the strike minus the premium, reached at an underlying of
    # zero, and the payoff engine is asked for precisely that number.
    summary = compute_payoff_summary(_short_put())

    assert summary.max_loss is not None, "a short put cannot lose more than the strike"
    expected = -(STRIKE - PREMIUM) * LOT  # -135,000
    assert summary.max_loss == pytest.approx(expected)
    assert summary.max_loss == pytest.approx(total_payoff(_short_put(), 0.0))


def test_a_long_puts_max_profit_is_bounded_and_exact():
    # The same truncation on the profit side: a long put cannot make more
    # than the strike minus what it cost, and that too used to read None.
    summary = compute_payoff_summary(_long_put())

    assert summary.max_profit is not None
    assert summary.max_profit == pytest.approx((STRIKE - PREMIUM) * LOT)
    assert summary.max_loss == pytest.approx(-PREMIUM * LOT)


def test_the_worst_sampled_loss_reaches_the_real_floor():
    # `worst_sampled_loss` is what `evaluate_options_risk` sizes an
    # unbounded combination by, so a window that stops halfway down
    # understates it even where `max_loss` is legitimately None. A short
    # straddle's loss runs away upward, but its downside is still worst at
    # zero.
    summary = compute_payoff_summary(_short_straddle())

    assert summary.max_loss is None, "the call leg really is unbounded upward"
    assert summary.worst_sampled_loss == pytest.approx(total_payoff(_short_straddle(), 0.0))
    # Pre-fix this was the right-hand edge, -45,000.
    assert summary.worst_sampled_loss == pytest.approx(-120_000.0)


def test_the_risk_gate_rejects_a_short_put_that_can_lose_more_than_the_account():
    # The harm, at the gate that is supposed to stop it. 135,000 of real
    # risk against a 100,000 account is 135% of the balance; the limit is
    # 100%. Pre-fix the gate was handed 60,000 -- the payoff at half the
    # strike -- and approved it.
    balance = 100_000.0
    payoff = compute_payoff_summary(_short_put())

    result = evaluate_options_risk(
        OptionsRiskProposal(
            account_id="acct",
            account_balance=balance,
            current_exposure=0.0,
            payoff=payoff,
            broker_healthy=True,
        )
    )

    assert result.decision.value == "REJECT"
    exposure = next(c for c in result.checks if c.name == "exposure_limit")
    assert not exposure.passed
    assert "135.00%" in exposure.detail

    # And the number really is the account-busting one, not merely "bigger":
    # the risk the gate sees must be the full strike-minus-premium figure.
    risk_seen = abs(payoff.max_loss)
    assert risk_seen == pytest.approx(135_000.0)
    assert risk_seen > balance


@pytest.mark.parametrize(
    "label, legs",
    [
        ("naked short call", [OptionLeg(OptionType.CALL, STRIKE, PREMIUM, 1, Direction.SHORT, LOT)]),
        ("short straddle", _short_straddle()),
        (
            "short strangle",
            [
                OptionLeg(OptionType.CALL, 1100.0, 60.0, 1, Direction.SHORT, LOT),
                OptionLeg(OptionType.PUT, 900.0, 60.0, 1, Direction.SHORT, LOT),
            ],
        ),
    ],
)
def test_genuinely_unbounded_losses_are_still_reported_as_unbounded(label, legs):
    # The control that matters most, and the one a naive version of this
    # fix breaks. Moving the window's floor to zero moves where the sampled
    # minimum *sits*: for a short straddle the worst sampled point becomes
    # the left edge, not the right. Boundedness therefore has to be settled
    # by the edge slope alone -- a payoff at expiry is piecewise linear and
    # its slope past the outermost strike is constant forever, so a right
    # edge still falling keeps falling. Requiring the sampled minimum to
    # also sit on that edge silently re-labels these as defined-risk.
    summary = compute_payoff_summary(legs)
    assert summary.max_loss is None, f"{label} loses without limit as the underlying rises"


def test_unlimited_upside_is_still_reported_as_unbounded():
    legs = [OptionLeg(OptionType.CALL, STRIKE, PREMIUM, 1, Direction.LONG, LOT)]
    summary = compute_payoff_summary(legs)

    assert summary.max_profit is None
    assert summary.max_loss == pytest.approx(-PREMIUM * LOT)


def test_defined_risk_spreads_are_unchanged():
    # The wider control: this fix must not move any number for the
    # bounded-both-ways strategies the suite already pins.
    legs = [
        OptionLeg(OptionType.PUT, 25000.0, 300.0, 1, Direction.SHORT, 50),
        OptionLeg(OptionType.PUT, 24500.0, 150.0, 1, Direction.LONG, 50),
    ]
    summary = compute_payoff_summary(legs)

    net_credit = (300.0 - 150.0) * 50
    assert summary.max_profit == pytest.approx(net_credit, rel=1e-3)
    assert summary.max_loss == pytest.approx(-((25000.0 - 24500.0) * 50 - net_credit), rel=1e-3)
    assert summary.max_loss == pytest.approx(total_payoff(legs, 0.0))


def test_an_explicit_range_that_stops_above_zero_still_reports_unbounded():
    # `price_range` is a public parameter, and a caller who supplies a
    # window that does not reach zero has genuinely truncated the curve --
    # the answer for that window is still "not bounded within it". Only a
    # range including zero earns the exact figure.
    legs = _short_put()
    truncated = [500.0 + i for i in range(501)]  # 500..1000, never reaching 0

    assert compute_payoff_summary(legs, truncated).max_loss is None
    assert compute_payoff_summary(legs).max_loss is not None


def test_capital_requirement_for_a_credit_strategy_reaches_the_real_floor():
    # `capital_requirement` falls through to `abs(min(payoffs))` for a net
    # credit, so it inherited the same truncated window -- and
    # `evaluate_options_risk` floors an unbounded strategy's risk at it.
    summary = compute_payoff_summary(_short_put())

    assert summary.net_premium < 0, "a sold put is a credit"
    assert summary.capital_requirement == pytest.approx((STRIKE - PREMIUM) * LOT)
