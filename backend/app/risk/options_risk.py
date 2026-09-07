"""Risk gate for multi-leg options strategies (blueprint §37-40, §56).

`app.risk.engine.TradeRiskProposal` is shaped around a single directional
trade — one entry price, one stop, a risk-per-unit computed from their
distance. A multi-leg, defined-risk options strategy (a spread, condor,
etc.) doesn't have that shape at all: its risk is whatever
`app.options.payoff.compute_payoff_summary` already computes for the
whole combination (`max_loss`/`capital_requirement`). Forcing it through
`TradeRiskProposal`'s entry/stop fields would mean faking a stop distance
that has no real meaning — this is a small, dedicated check instead,
reusing the same kill-switch/exposure/liquidity/health primitives.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.options.payoff import PayoffResult
from app.risk.kill_switch import KillSwitchState
from app.risk.limits import RiskCheck, RiskDecision, RiskDecisionResult, RiskLimits


@dataclass(slots=True)
class OptionsRiskProposal:
    account_id: str
    account_balance: float
    current_exposure: float  # notional already at risk elsewhere, in account currency
    payoff: PayoffResult
    broker_healthy: bool
    # Blueprint §56/§57: the same account-wide capital-preservation and
    # circuit-breaker controls `app.risk.engine.TradeRiskProposal` already
    # enforces for every other order-entry path (POST /orders,
    # PaperTradingEngine, AutoTradeSupervisor). An options strategy is a
    # real trade placed through the same broker/persistence pipeline (see
    # this proposal's only caller, app.api.options.execute_options_strategy)
    # and must be bound by the same daily/weekly loss halt, open-position
    # cap, and per-day trade cap -- not exempt from them just because its
    # risk shape (a payoff curve, not a single entry/stop) doesn't fit
    # TradeRiskProposal.
    open_positions: int = 0
    trades_today: int = 0
    daily_pnl: float = 0.0  # negative = loss
    weekly_pnl: float = 0.0
    repeated_rejections: int = 0
    market_data_age_seconds: float = 0.0
    liquidity_acceptable: bool = True
    # Worst (max) percent gap, across every leg with a real OptionSnapshot
    # quote available, between that leg's client-claimed `premium` and the
    # snapshot's own bid/ask mid -- 0.0 when no leg has snapshot data yet.
    # See RiskLimits.max_premium_deviation_pct for why this exists: premium
    # is otherwise trusted input that sizes this strategy's own payoff/risk
    # math (compute_payoff_summary), unchecked against anything real.
    premium_deviation_pct: float = 0.0


def evaluate_options_risk(
    proposal: OptionsRiskProposal, limits: RiskLimits | None = None, kill_switch: KillSwitchState | None = None
) -> RiskDecisionResult:
    limits = limits or RiskLimits()
    kill_switch = kill_switch or KillSwitchState()
    checks: list[RiskCheck] = []

    kill_reason = kill_switch.is_blocked(proposal.account_id, None)
    checks.append(RiskCheck("kill_switch", kill_reason is None, kill_reason or ""))
    if kill_reason is not None:
        return RiskDecisionResult(RiskDecision.REJECT, checks, kill_reason)

    # Worst-case loss for this strategy: the defined max_loss when the
    # payoff curve bounds it; otherwise the worst P&L the payoff engine
    # actually sampled, floored at the capital committed — never treat
    # "unbounded" as "zero risk".
    #
    # This used to read `capital_requirement`, which does not mean what
    # this check needs. `compute_payoff_summary` sets it to the net debit
    # whenever the strategy is one (`max(premium, 0.0) or abs(min(payoffs))`
    # in app/options/payoff.py), falling through to the worst sampled loss
    # only for a net *credit*. So an unbounded-risk combination entered for
    # a debit was sized by its entry cost: a long 25000 put plus a short
    # 26000 call at lot size 50 is a synthetic short with unlimited loss
    # above 26000, and a 1,000 debit made it "1.00% of a 100,000 account"
    # against a curve already 651,000 underwater at the edge of the sampled
    # range. Raising one leg's premium by 10 turned the same position into a
    # 500 credit and the check into "649.50%" -- a rejection. Identical
    # risk, opposite decision, decided by which side of zero the premium
    # happened to land on.
    payoff = proposal.payoff
    risk_amount = (
        abs(payoff.max_loss)
        if payoff.max_loss is not None
        else max(-payoff.worst_sampled_loss, payoff.capital_requirement)
    )

    projected_exposure_pct = (
        (proposal.current_exposure + risk_amount) / proposal.account_balance * 100 if proposal.account_balance else 100.0
    )
    checks.append(
        RiskCheck(
            "exposure_limit",
            projected_exposure_pct <= limits.max_exposure_pct,
            f"Projected exposure {projected_exposure_pct:.2f}% vs limit {limits.max_exposure_pct}%",
        )
    )

    daily_loss_pct = max(-proposal.daily_pnl, 0) / proposal.account_balance * 100 if proposal.account_balance else 0
    checks.append(
        RiskCheck(
            "daily_loss_limit",
            daily_loss_pct < limits.max_daily_loss_pct,
            f"Daily loss {daily_loss_pct:.2f}% vs limit {limits.max_daily_loss_pct}%",
        )
    )
    weekly_loss_pct = max(-proposal.weekly_pnl, 0) / proposal.account_balance * 100 if proposal.account_balance else 0
    checks.append(
        RiskCheck(
            "weekly_loss_limit",
            weekly_loss_pct < limits.max_weekly_loss_pct,
            f"Weekly loss {weekly_loss_pct:.2f}% vs limit {limits.max_weekly_loss_pct}%",
        )
    )
    checks.append(
        RiskCheck(
            "max_open_positions",
            proposal.open_positions < limits.max_open_positions,
            f"{proposal.open_positions} open vs limit {limits.max_open_positions}",
        )
    )
    checks.append(
        RiskCheck(
            "max_trades_per_day",
            proposal.trades_today < limits.max_trades_per_day,
            f"{proposal.trades_today} trades today vs limit {limits.max_trades_per_day}",
        )
    )
    checks.append(
        RiskCheck(
            "no_repeated_rejections",
            proposal.repeated_rejections < limits.max_repeated_rejections,
        )
    )
    checks.append(RiskCheck("liquidity_acceptable", proposal.liquidity_acceptable))
    checks.append(
        RiskCheck(
            "premium_matches_market",
            proposal.premium_deviation_pct <= limits.max_premium_deviation_pct,
            f"Premium deviates {proposal.premium_deviation_pct:.2f}% from the real quote vs limit {limits.max_premium_deviation_pct}%",
        )
    )
    checks.append(
        RiskCheck(
            "market_data_fresh",
            proposal.market_data_age_seconds <= limits.market_data_max_staleness_seconds,
            f"Data age {proposal.market_data_age_seconds}s vs max {limits.market_data_max_staleness_seconds}s",
        )
    )
    checks.append(RiskCheck("broker_healthy", proposal.broker_healthy))

    failed = [c for c in checks if not c.passed]
    if failed:
        return RiskDecisionResult(RiskDecision.REJECT, checks, failed[0].detail or failed[0].name)
    return RiskDecisionResult(RiskDecision.APPROVE, checks, None)
