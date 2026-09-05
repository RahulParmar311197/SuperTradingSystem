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
    # payoff curve bounds it, otherwise compute_payoff_summary's own
    # capital_requirement (already its best estimate of worst-case loss
    # for an unbounded-risk combination — never treat "unbounded" as
    # "zero risk").
    risk_amount = abs(proposal.payoff.max_loss) if proposal.payoff.max_loss is not None else proposal.payoff.capital_requirement

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
