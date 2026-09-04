"""Strategy/setup scoring (blueprint §82-83). This is a ranking heuristic
built from measurable conditions — never a probability of winning."""

from __future__ import annotations

from app.strategy.context import EvaluationContext
from app.strategy.dsl import ConditionType

DEFAULT_WEIGHTS: dict[str, float] = {
    "htf_alignment": 20.0,
    "structure": 20.0,
    "liquidity": 20.0,
    "fvg": 15.0,
    "volatility": 10.0,
    "risk_reward": 15.0,
}

_STRUCTURE_TYPES = {ConditionType.BOS, ConditionType.MSS, ConditionType.CHOCH}


def compute_strategy_score(
    context: EvaluationContext,
    satisfied_condition_types: list[ConditionType],
    risk_reward: float,
    minimum_rr: float,
    weights: dict[str, float] | None = None,
) -> float:
    weights = weights or DEFAULT_WEIGHTS
    satisfied = set(satisfied_condition_types)
    total_weight = sum(weights.values()) or 1.0
    score = 0.0

    if ConditionType.TREND in satisfied:
        score += weights.get("htf_alignment", 0.0)
    if satisfied & _STRUCTURE_TYPES:
        score += weights.get("structure", 0.0)
    if ConditionType.LIQUIDITY_SWEEP in satisfied:
        score += weights.get("liquidity", 0.0)
    if ConditionType.FVG in satisfied or ConditionType.ORDER_BLOCK in satisfied:
        score += weights.get("fvg", 0.0)
    if ConditionType.VOLATILITY in satisfied:
        score += weights.get("volatility", 0.0)
    if minimum_rr > 0 and risk_reward >= minimum_rr:
        rr_component = min(risk_reward / minimum_rr, 1.5) / 1.5
        score += weights.get("risk_reward", 0.0) * rr_component

    return round(min(score, total_weight) / total_weight * 100, 2)
