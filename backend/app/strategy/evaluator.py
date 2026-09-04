"""Evaluates Strategy DSL conditions against an EvaluationContext.

Each SMC/ICT-derived condition type looks at the already-computed
SMCContext/ICTContext (no re-computation here), so evaluation is cheap and
inherits the look-ahead guarantees of those engines. Numeric condition
types (volume, volatility, indicator, options_*) compare against the
generic `indicators` bag using the DSL's operator/value fields.
"""

from __future__ import annotations

from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionOperator, ConditionType


def _numeric_compare(actual: float | None, condition: Condition) -> bool:
    if actual is None:
        return False
    op = condition.operator or ConditionOperator.GREATER_THAN
    if op == ConditionOperator.GREATER_THAN:
        return condition.value is not None and actual > condition.value
    if op == ConditionOperator.LESS_THAN:
        return condition.value is not None and actual < condition.value
    if op == ConditionOperator.WITHIN:
        return (
            condition.min_value is not None
            and condition.max_value is not None
            and condition.min_value <= actual <= condition.max_value
        )
    if op in (ConditionOperator.TOUCHES, ConditionOperator.CROSSES):
        # Without prior-value history we treat touch/cross as "within a small band"
        # of the target value — callers wanting true cross detection should feed
        # a precomputed boolean into `indicators` instead.
        return condition.value is not None and abs(actual - condition.value) <= abs(condition.value) * 0.001
    return False


def evaluate_condition(condition: Condition, context: EvaluationContext) -> bool:
    smc = context.smc
    ict = context.ict

    match condition.type:
        case ConditionType.TREND:
            return smc.bias is not None and (
                condition.direction is None or smc.bias.lower() == condition.direction.lower()
            )

        case ConditionType.BOS | ConditionType.MSS | ConditionType.CHOCH:
            event_name = condition.type.value.upper()
            events = smc.mss_events if condition.type == ConditionType.MSS else smc.structure_events
            matches = [
                e
                for e in events
                if e.event_type.value == event_name
                and (condition.direction is None or e.direction.value.lower() == condition.direction.lower())
            ]
            return len(matches) > 0

        case ConditionType.LIQUIDITY_SWEEP:
            side = f"{condition.side.upper()}_SIDE" if condition.side else None
            sweeps = smc.recent_sweeps(side=side)
            return len(sweeps) > 0

        case ConditionType.FVG:
            direction = condition.direction.upper() if condition.direction else None
            return len(smc.unmitigated_fvgs(direction=direction)) > 0

        case ConditionType.ORDER_BLOCK:
            direction = condition.direction.upper() if condition.direction else None
            return len(smc.active_order_blocks(direction=direction)) > 0

        case ConditionType.PREMIUM_DISCOUNT:
            if smc.current_zone is None:
                return False
            return condition.zone is not None and smc.current_zone.lower() == condition.zone.lower()

        case ConditionType.SESSION:
            if condition.name:
                return condition.name.upper() in ict.current_kill_zones
            return len(ict.current_kill_zones) > 0

        case ConditionType.VOLUME | ConditionType.VOLATILITY | ConditionType.INDICATOR:
            key = condition.name or condition.type.value
            return _numeric_compare(context.indicators.get(key), condition)

        case ConditionType.OPTIONS_IV | ConditionType.OPTIONS_OI | ConditionType.OPTIONS_GREEKS:
            key = condition.name or condition.type.value
            return _numeric_compare(context.indicators.get(key), condition)

    return False


def evaluate_conditions(
    conditions: list[Condition], context: EvaluationContext
) -> tuple[bool, list[str], list[str]]:
    """Implicit AND across the list (blueprint §34 example). Returns
    (all_satisfied, satisfied_labels, missing_labels)."""
    satisfied: list[str] = []
    missing: list[str] = []

    for condition in conditions:
        label = condition.name or condition.type.value
        if evaluate_condition(condition, context):
            satisfied.append(label)
        else:
            missing.append(label)

    return len(missing) == 0, satisfied, missing
