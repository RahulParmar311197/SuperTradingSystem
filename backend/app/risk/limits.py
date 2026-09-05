"""User- and system-level risk controls (blueprint §57) and the shape of a
risk decision (blueprint §56)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass(slots=True)
class RiskLimits:
    # User-configurable
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 2.0
    max_weekly_loss_pct: float = 5.0
    max_open_positions: int = 5
    max_trades_per_day: int = 10
    max_exposure_pct: float = 100.0
    max_position_size: float | None = None
    max_strategy_allocation_pct: float = 100.0
    # Blueprint §85 "Correlation Engine": reject a new position when its
    # notional plus every existing position correlated with it (at or
    # above correlation_threshold) exceeds this share of the account.
    # 100% is a no-op default — correlation data isn't always available
    # (see app.risk.correlation), and this must never silently block
    # trading where it hasn't been computed.
    max_correlated_exposure_pct: float = 100.0
    correlation_threshold: float = 0.7

    # System-level
    market_data_max_staleness_seconds: float = 10.0
    max_repeated_rejections: int = 3
    max_price_jump_pct: float = 5.0
    # A live order's `entry` is client-supplied and only used for position
    # sizing / notional risk math -- the broker fills a MARKET order at its
    # own real price, independent of it. Without a check tying the two
    # together, a client could submit an `entry` deliberately close to
    # `stop` to inflate the sized quantity (risk_amount / |entry - stop|)
    # while the notional-based checks below, computed as quantity * entry,
    # stay small because they use that same manipulated `entry` -- passing
    # every exposure check even though the broker fills the real,
    # much-larger quantity at the real market price. See
    # TradeRiskProposal.entry_deviation_pct.
    max_entry_deviation_pct: float = 1.0


class RiskDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


@dataclass(slots=True)
class RiskCheck:
    name: str
    passed: bool
    detail: str = ""


@dataclass(slots=True)
class RiskDecisionResult:
    decision: RiskDecision
    checks: list[RiskCheck]
    reason: str | None = None

    @property
    def approved(self) -> bool:
        return self.decision == RiskDecision.APPROVE

    @property
    def failed_checks(self) -> list[RiskCheck]:
        return [c for c in self.checks if not c.passed]
