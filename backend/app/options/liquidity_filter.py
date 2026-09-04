"""Options liquidity filter (blueprint §40) — essential for automated
options execution: reject or warn about poor-quality contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(slots=True)
class LiquidityFilterConfig:
    min_volume: float = 100.0
    min_open_interest: float = 500.0
    max_spread_pct: float = 5.0  # (ask - bid) / mid * 100
    max_quote_age_seconds: float = 30.0


@dataclass(slots=True)
class LiquidityAssessment:
    acceptable: bool
    warnings: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)


def evaluate_liquidity(
    volume: float,
    open_interest: float,
    bid: float | None,
    ask: float | None,
    quote_timestamp: datetime | None,
    config: LiquidityFilterConfig | None = None,
    now: datetime | None = None,
) -> LiquidityAssessment:
    config = config or LiquidityFilterConfig()
    now = now or datetime.now(timezone.utc)
    rejections: list[str] = []
    warnings: list[str] = []

    if volume < config.min_volume:
        rejections.append(f"Volume {volume} below minimum {config.min_volume}")
    if open_interest < config.min_open_interest:
        rejections.append(f"Open interest {open_interest} below minimum {config.min_open_interest}")

    if bid is not None and ask is not None and bid > 0 and ask > 0:
        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid else float("inf")
        if spread_pct > config.max_spread_pct:
            rejections.append(f"Spread {spread_pct:.2f}% exceeds maximum {config.max_spread_pct}%")
    else:
        warnings.append("Missing bid/ask — cannot evaluate spread")

    if quote_timestamp is not None:
        age = (now - quote_timestamp).total_seconds()
        if age > config.max_quote_age_seconds:
            rejections.append(f"Quote is {age:.1f}s old, exceeds maximum {config.max_quote_age_seconds}s")

    return LiquidityAssessment(acceptable=len(rejections) == 0, warnings=warnings, rejections=rejections)
