import pytest
from pydantic import ValidationError

from app.strategy.dsl import Condition, ConditionOperator, ConditionType


@pytest.mark.parametrize("operator", [ConditionOperator.AND, ConditionOperator.OR, ConditionOperator.NOT])
def test_condition_rejects_unimplemented_boolean_operators(operator):
    # Regression test: `ConditionOperator` declares AND/OR/NOT (blueprint
    # §33 lists them), and `Condition.operator` accepted any of them with
    # no further validation -- but app/strategy/evaluator.py's
    # `_numeric_compare` (the only reader of `Condition.operator`) never
    # implemented any of the three. A strategy using one -- and the AI
    # strategy builder's own system prompt explicitly permits using any
    # operator "the schema defines" -- passed validation cleanly but its
    # condition silently fell through to `evaluate_condition`'s final
    # `return False` on every candle forever: a strategy that could
    # structurally never fire, with no error anywhere indicating why.
    with pytest.raises(ValidationError, match="not yet implemented"):
        Condition(type=ConditionType.INDICATOR, name="rsi", operator=operator, min_value=40, max_value=60)


@pytest.mark.parametrize(
    "operator", [ConditionOperator.GREATER_THAN, ConditionOperator.LESS_THAN, ConditionOperator.WITHIN]
)
def test_condition_accepts_implemented_operators(operator):
    condition = Condition(type=ConditionType.INDICATOR, name="rsi", operator=operator, value=50, min_value=40, max_value=60)
    assert condition.operator == operator
