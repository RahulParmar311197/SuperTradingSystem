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


def test_entry_deviation_defaults_to_a_no_op():
    # entry_deviation_pct defaults to 0.0 -- a clean trade must still
    # approve even though max_entry_deviation_pct exists.
    result = RiskEngine().evaluate(_base_proposal())
    assert any(c.name == "entry_matches_market" and c.passed for c in result.checks)


def test_rejects_when_entry_deviates_from_the_real_market_quote():
    # Regression test: a client-supplied `entry` is otherwise trusted
    # input used both to size the position (calculate_position_size) and
    # to size that same position's notional risk checks (exposure_limit et
    # al., computed as quantity * entry). Picking an `entry` close to
    # `stop` inflates quantity while those notional checks -- computed
    # from that same forged entry -- still look small, letting an
    # oversized order clear every exposure check before the broker fills
    # the real quantity at its own, unrelated real price. This check
    # exists specifically to catch that gap.
    proposal = _base_proposal(entry_deviation_pct=5.0)  # default limit is 1.0%
    result = RiskEngine().evaluate(proposal)
    assert result.decision == RiskDecision.REJECT
    assert any(c.name == "entry_matches_market" and not c.passed for c in result.checks)


def _tripped_proposal(**overrides) -> TradeRiskProposal:
    """A proposal that fails every entry-only limit at once."""
    defaults = dict(
        account_id="acct-1",
        strategy_id=None,
        entry=100.0,
        stop=95.0,
        account_balance=100_000.0,
        open_positions=99,
        trades_today=99,
        daily_pnl=-50_000.0,
        weekly_pnl=-50_000.0,
        current_exposure=10_000_000.0,
        strategy_allocation=10_000_000.0,
        correlated_exposure=10_000_000.0,
        market_data_age_seconds=0.0,
        broker_healthy=True,
    )
    defaults.update(overrides)
    return TradeRiskProposal(**defaults)


def test_a_reducing_proposal_skips_only_the_entry_limits():
    # An order that can only reduce an existing position takes on no new
    # risk, so the limits that cap risk-taking must not block it -- but
    # everything about whether *this* order can execute sanely right now
    # still applies. Pinning the exact split, not just "it was approved":
    # a future check added to the wrong side of the fence is the failure
    # mode this guards.
    entry_only = {
        "daily_loss_limit",
        "weekly_loss_limit",
        "exposure_limit",
        "strategy_allocation_limit",
        "correlated_exposure_limit",
        "max_open_positions",
        "max_trades_per_day",
    }
    always = {
        "kill_switch",
        "valid_stop_distance",
        "entry_matches_market",
        "market_data_fresh",
        "broker_healthy",
        "no_repeated_rejections",
        "no_abnormal_price_jump",
    }
    # `liquidity_acceptable` is deliberately absent: `_tripped_proposal`
    # does not supply one, and an unsupplied assessment is now skipped
    # rather than recorded as passed. This set previously listed it, which
    # encoded the bug -- no caller of this engine has ever supplied a
    # liquidity assessment, so the name appeared in every audit row with
    # nothing behind it. A proposal that *does* supply one still runs the
    # check on both paths; see the three tests below.

    entry_decision = RiskEngine().evaluate(_tripped_proposal())
    assert entry_decision.decision == RiskDecision.REJECT
    assert entry_only <= {c.name for c in entry_decision.checks}

    reducing = RiskEngine().evaluate(_tripped_proposal(is_reducing=True))
    assert reducing.decision == RiskDecision.APPROVE
    names = {c.name for c in reducing.checks}
    assert names == always, "a reducing order must run exactly the execution-sanity checks"
    assert not (names & entry_only)


def test_a_reducing_proposal_is_still_stopped_by_the_kill_switch():
    # The kill switch is a deliberate human stop, not an exposure limit:
    # it must hold against every order, exits included.
    kill_switch = KillSwitchState()
    kill_switch.kill_global()
    decision = RiskEngine(kill_switch=kill_switch).evaluate(_tripped_proposal(is_reducing=True))
    assert decision.decision == RiskDecision.REJECT
    assert "Global kill switch" in decision.reason


def test_a_reducing_proposal_is_still_stopped_by_an_unhealthy_broker():
    decision = RiskEngine().evaluate(_tripped_proposal(is_reducing=True, broker_healthy=False))
    assert decision.decision == RiskDecision.REJECT
    assert any(c.name == "broker_healthy" and not c.passed for c in decision.checks)


def test_no_market_data_at_all_fails_the_freshness_check():
    # `None` is not a small age, it is the absence of one. Flattened to
    # 0.0 it read as the freshest possible value, so the staleness gate
    # passed for an instrument with no feed -- while a real 60s age
    # against a 10s limit was rejected. Worse information must not pass a
    # gate that better information fails.
    decision = RiskEngine(limits=RiskLimits()).evaluate(_base_proposal(market_data_age_seconds=None))

    assert not decision.approved
    check = next(c for c in decision.checks if c.name == "market_data_fresh")
    assert not check.passed
    assert check.detail == "No market data for this instrument"


def test_a_caller_that_knows_the_data_is_fresh_still_passes():
    decision = RiskEngine(limits=RiskLimits()).evaluate(_base_proposal(market_data_age_seconds=0.0))

    assert next(c for c in decision.checks if c.name == "market_data_fresh").passed


# --- liquidity_acceptable: evaluated, not evaluated, and failed ----------


def test_an_unevaluated_liquidity_assessment_is_not_recorded_as_a_passed_check():
    # The bug. `liquidity_acceptable` defaulted to True and neither
    # app/api/orders.py nor app/paper/engine.py has ever set it, so every
    # equity order's RiskEvent recorded `liquidity_acceptable: true`
    # without anything having looked at volume, spread or quote age.
    # "Not assessed" and "assessed and fine" are different facts and the
    # audit row must not spell them the same way.
    decision = RiskEngine().evaluate(_base_proposal())

    assert decision.decision == RiskDecision.APPROVE
    assert "liquidity_acceptable" not in {c.name for c in decision.checks}


def test_an_unevaluated_liquidity_assessment_does_not_block_the_order():
    # Control, and the reason this is a skip rather than a rejection: a
    # gate nobody has wired up must not stop trading -- the same choice
    # `max_correlated_exposure_pct`'s 100.0 no-op default makes.
    decision = RiskEngine().evaluate(_base_proposal())
    assert decision.decision == RiskDecision.APPROVE
    assert decision.reason is None


def test_a_caller_that_assesses_liquidity_gets_a_real_gate():
    # The capability has to survive the fix, or this traded a false audit
    # entry for a missing one. A caller that does the work and reports
    # unacceptable liquidity must be rejected, and one that reports
    # acceptable must have that recorded as a genuinely passed check.
    rejected = RiskEngine().evaluate(_base_proposal(liquidity_acceptable=False))
    assert rejected.decision == RiskDecision.REJECT
    assert any(c.name == "liquidity_acceptable" and not c.passed for c in rejected.checks)

    approved = RiskEngine().evaluate(_base_proposal(liquidity_acceptable=True))
    assert approved.decision == RiskDecision.APPROVE
    assert any(c.name == "liquidity_acceptable" and c.passed for c in approved.checks)


def test_liquidity_is_an_execution_sanity_check_not_an_entry_only_limit():
    # An assessed-and-unacceptable symbol is illiquid whether the order
    # opens or closes a position, so unlike the exposure/loss/count caps
    # this one must still run for a reducing order. Pinning the side of
    # the fence, the same way the reducing-order test above does.
    decision = RiskEngine().evaluate(_tripped_proposal(is_reducing=True, liquidity_acceptable=False))
    assert decision.decision == RiskDecision.REJECT
    assert any(c.name == "liquidity_acceptable" and not c.passed for c in decision.checks)


# --- the correlated-exposure gate reads direction ---------------------------
#
# Worth recording before any of these: at the shipped defaults this gate
# is INERT. `max_correlated_exposure_pct` and `max_exposure_pct` are both
# 100.0, and correlated exposure is a subset of gross exposure, so the
# correlated check cannot fail unless `exposure_limit` already has --
# probed over 20,000 random books, 0 independent failures. These tests
# therefore tighten the limit, which is the only configuration in which
# the gate does anything at all.

_HEDGE_LIMITS = RiskLimits(max_correlated_exposure_pct=60.0, max_exposure_pct=1000.0)


def _correlated_check(proposal, limits=_HEDGE_LIMITS):
    result = RiskEngine(limits=limits).evaluate(proposal)
    return next(c for c in result.checks if c.name == "correlated_exposure_limit")


def test_a_hedge_passes_a_tightened_correlation_gate():
    """Behavioural proof, end to end through the engine. A long of 100,000
    against a correlated short of the same size nets to nothing.
    """
    check = _correlated_check(
        _base_proposal(
            entry=100.0, stop=95.0,               # long
            proposed_quantity=1000.0,             # 100,000 notional
            correlated_exposure=-100_000.0,       # a correlated short
            current_exposure=100_000.0,
        )
    )
    assert check.passed, check.detail


def test_a_one_way_correlated_book_still_fails_it():
    """Control. Identical to the hedge above but with the sibling long, so
    the two stack into one 200% bet. If this ever passes, signing the gate
    turned it off rather than making it read direction.
    """
    check = _correlated_check(
        _base_proposal(
            entry=100.0, stop=95.0,
            proposed_quantity=1000.0,
            correlated_exposure=+100_000.0,
            current_exposure=100_000.0,
        )
    )
    assert not check.passed, check.detail


def test_the_gate_reads_the_proposed_trades_own_direction():
    """Behavioural proof for `signed_target_notional` specifically.

    Same book both times -- one correlated long already open. Going long
    alongside it concentrates; going short against it hedges. Nothing but
    the proposed trade's own stop placement differs, which is how the
    engine knows the direction (it has already rejected entry == stop).
    """
    long_side = _correlated_check(
        _base_proposal(entry=100.0, stop=95.0, proposed_quantity=1000.0,
                       correlated_exposure=+100_000.0, current_exposure=100_000.0)
    )
    short_side = _correlated_check(
        _base_proposal(entry=100.0, stop=105.0, proposed_quantity=1000.0,
                       correlated_exposure=+100_000.0, current_exposure=100_000.0)
    )
    assert not long_side.passed, long_side.detail
    assert short_side.passed, short_side.detail


def test_netting_does_not_leak_into_the_gross_exposure_limit():
    """Control, and the reason netting is safe here at all.

    Netting a correlated book is only defensible because gross size is
    capped separately: an estimated correlation can break exactly when it
    is being relied on, so a book that nets to zero must still not be
    allowed to grow without bound. `exposure_limit` reads
    `current_exposure`, which is unsigned at both call sites and untouched
    by any of this. A perfectly hedged book must still consume its full
    gross allowance.
    """
    limits = RiskLimits(max_exposure_pct=150.0, max_correlated_exposure_pct=60.0)
    result = RiskEngine(limits=limits).evaluate(
        _base_proposal(
            entry=100.0, stop=95.0,
            proposed_quantity=1000.0,        # 100,000 -> 100% of balance
            current_exposure=100_000.0,      # already 100% gross
            correlated_exposure=-100_000.0,  # ... and perfectly hedged
        )
    )
    gross = next(c for c in result.checks if c.name == "exposure_limit")
    correlated = next(c for c in result.checks if c.name == "correlated_exposure_limit")
    assert correlated.passed, "the hedge should clear the concentration gate"
    assert not gross.passed, (
        "200% gross must still fail a 150% gross limit -- netting is a "
        f"concentration measure, not a size exemption: {gross.detail}"
    )


def test_two_correlated_shorts_are_as_concentrated_as_two_longs():
    """Control on the magnitude, not just the netting.

    A book leaning hard short is exactly as concentrated as one leaning
    hard long; only the sign of the net differs. Without the `abs()` in
    the engine a short-leaning book produces a *negative* percentage,
    which slips under any positive limit -- a gate that only works in one
    direction, which is worse than no gate because it reads as one.
    """
    check = _correlated_check(
        _base_proposal(
            entry=100.0, stop=105.0,          # short
            proposed_quantity=1000.0,
            correlated_exposure=-100_000.0,   # a correlated short already open
            current_exposure=100_000.0,
        )
    )
    assert not check.passed, check.detail
    assert "200.00%" in check.detail, check.detail
