"""Risk engine (blueprint §56): the only authority that can veto a trade.

`AI ≠ Risk Manager` (blueprint §131) — this engine never consults the AI
and never trusts it; it only looks at deterministic account/market state.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.risk.kill_switch import KillSwitchState
from app.risk.limits import RiskCheck, RiskDecision, RiskDecisionResult, RiskLimits


@dataclass(slots=True)
class TradeRiskProposal:
    account_id: str
    strategy_id: str | None
    entry: float
    stop: float
    account_balance: float

    open_positions: int
    trades_today: int
    daily_pnl: float  # negative = loss
    weekly_pnl: float
    current_exposure: float  # in account-currency notional
    strategy_allocation: float  # notional already allocated to this strategy

    market_data_age_seconds: float
    broker_healthy: bool
    repeated_rejections: int = 0
    recent_price_jump_pct: float = 0.0
    liquidity_acceptable: bool = True

    proposed_quantity: float | None = None  # if None, engine sizes the position


def calculate_position_size(
    account_balance: float, risk_percent: float, entry: float, stop: float, max_position_size: float | None = None
) -> float:
    risk_per_unit = abs(entry - stop)
    if risk_per_unit <= 0:
        return 0.0
    risk_amount = account_balance * (risk_percent / 100)
    quantity = risk_amount / risk_per_unit
    if max_position_size is not None:
        quantity = min(quantity, max_position_size)
    return max(quantity, 0.0)


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None, kill_switch: KillSwitchState | None = None) -> None:
        self.limits = limits or RiskLimits()
        self.kill_switch = kill_switch or KillSwitchState()

    def evaluate(self, proposal: TradeRiskProposal) -> RiskDecisionResult:
        checks: list[RiskCheck] = []
        limits = self.limits

        kill_reason = self.kill_switch.is_blocked(proposal.account_id, proposal.strategy_id)
        checks.append(RiskCheck("kill_switch", kill_reason is None, kill_reason or ""))
        if kill_reason is not None:
            return RiskDecisionResult(RiskDecision.REJECT, checks, kill_reason)

        risk_per_unit = abs(proposal.entry - proposal.stop)
        checks.append(RiskCheck("valid_stop_distance", risk_per_unit > 0, "Entry and stop must differ"))
        if risk_per_unit <= 0:
            return RiskDecisionResult(RiskDecision.REJECT, checks, "Entry and stop must differ")

        quantity = proposal.proposed_quantity
        if quantity is None:
            quantity = calculate_position_size(
                proposal.account_balance, limits.risk_per_trade_pct, proposal.entry, proposal.stop, limits.max_position_size
            )
        position_notional = quantity * proposal.entry

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

        projected_exposure_pct = (
            (proposal.current_exposure + position_notional) / proposal.account_balance * 100
            if proposal.account_balance
            else 100.0
        )
        checks.append(
            RiskCheck(
                "exposure_limit",
                projected_exposure_pct <= limits.max_exposure_pct,
                f"Projected exposure {projected_exposure_pct:.2f}% vs limit {limits.max_exposure_pct}%",
            )
        )

        strategy_allocation_pct = (
            (proposal.strategy_allocation + position_notional) / proposal.account_balance * 100
            if proposal.account_balance
            else 100.0
        )
        checks.append(
            RiskCheck(
                "strategy_allocation_limit",
                strategy_allocation_pct <= limits.max_strategy_allocation_pct,
                f"Strategy allocation {strategy_allocation_pct:.2f}% vs limit {limits.max_strategy_allocation_pct}%",
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
        checks.append(RiskCheck("liquidity_acceptable", proposal.liquidity_acceptable))
        checks.append(
            RiskCheck(
                "market_data_fresh",
                proposal.market_data_age_seconds <= limits.market_data_max_staleness_seconds,
                f"Data age {proposal.market_data_age_seconds}s vs max {limits.market_data_max_staleness_seconds}s",
            )
        )
        checks.append(RiskCheck("broker_healthy", proposal.broker_healthy))
        checks.append(
            RiskCheck(
                "no_repeated_rejections",
                proposal.repeated_rejections < limits.max_repeated_rejections,
            )
        )
        checks.append(
            RiskCheck(
                "no_abnormal_price_jump",
                proposal.recent_price_jump_pct <= limits.max_price_jump_pct,
            )
        )

        failed = [c for c in checks if not c.passed]
        if failed:
            return RiskDecisionResult(RiskDecision.REJECT, checks, failed[0].detail or failed[0].name)
        return RiskDecisionResult(RiskDecision.APPROVE, checks, None)
