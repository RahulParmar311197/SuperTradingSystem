"""AI trade-proposal validation (blueprint §81).

The AI may *propose* a trade, but every number it states is cross-checked
against the deterministic `StrategyEvaluationResult` computed by
`app.strategy.engine` — the AI is never trusted to have computed entry/
stop/RR correctly on its own (§32, §131 "AI ≠ Final Authority").
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.strategy.engine import StrategyEvaluationResult

_PRICE_TOLERANCE_PCT = 0.5  # AI-stated prices may drift this much from the computed ones


@dataclass(slots=True)
class AIValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def _within_tolerance(a: float, b: float, tolerance_pct: float = _PRICE_TOLERANCE_PCT) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) * 100 <= tolerance_pct


def validate_ai_trade_proposal(
    proposal: dict,
    deterministic_result: StrategyEvaluationResult,
    instrument_tradable: bool,
    max_risk_percent: float,
) -> AIValidationResult:
    errors: list[str] = []

    if not deterministic_result.matched:
        errors.append("No signal exists: the strategy conditions are not currently satisfied")
        return AIValidationResult(valid=False, errors=errors)

    proposed_direction = str(proposal.get("direction", "")).lower()
    if proposed_direction != (deterministic_result.direction or "").lower():
        errors.append(
            f"Proposed direction '{proposed_direction}' does not match the detected setup "
            f"'{deterministic_result.direction}'"
        )

    entry = proposal.get("entry")
    if entry is None or not _within_tolerance(float(entry), deterministic_result.entry):
        errors.append(f"Proposed entry {entry} does not match the computed entry {deterministic_result.entry}")

    stop = proposal.get("stop")
    if stop is None or not _within_tolerance(float(stop), deterministic_result.stop):
        errors.append(f"Proposed stop {stop} does not match the computed stop {deterministic_result.stop}")

    risk_reward = proposal.get("risk_reward")
    if risk_reward is None or float(risk_reward) < deterministic_result.risk_reward - 1e-6:
        errors.append(
            f"Proposed risk/reward {risk_reward} is below the computed {deterministic_result.risk_reward}"
        )

    risk_percent = proposal.get("risk_percent")
    if risk_percent is None or float(risk_percent) <= 0 or float(risk_percent) > max_risk_percent:
        errors.append(f"Proposed risk_percent {risk_percent} exceeds the maximum allowed {max_risk_percent}%")

    if not instrument_tradable:
        errors.append("Instrument is not currently tradable")

    return AIValidationResult(valid=len(errors) == 0, errors=errors)
