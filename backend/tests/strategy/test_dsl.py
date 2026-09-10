import pytest
from pydantic import ValidationError

from app.strategy.dsl import Condition, ConditionOperator, ConditionType, StrategyDefinition


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


@pytest.mark.parametrize("direction", ["LONG", "SHORT", "long", "short", "up", "down", "", "  ", "bulish"])
def test_strategy_definition_rejects_directions_outside_the_bias_vocabulary(direction):
    # Regression test: `direction` was a bare `str` read as
    # `direction.lower() == "bullish"` in app/strategy/engine.py,
    # app/paper/engine.py and app/backtest/engine.py, so every other value
    # -- including "" -- silently meant *bearish*. A strategy declared
    # "LONG" evaluated to `entry=100 stop=120 target=60` on a 90-120
    # dealing range: stop above entry, target below, and the paper and
    # autonomous engines opened a short from it.
    #
    # "LONG" is not a far-fetched typo. This platform's own order
    # endpoints take the identically-named `direction` key with the other
    # vocabulary and enum-validate it, so POST /orders refuses "bullish"
    # while POST /strategies accepted "LONG" and traded it short.
    with pytest.raises(ValidationError):
        StrategyDefinition(name="S", market="X", timeframe="15m", direction=direction)


@pytest.mark.parametrize("direction", ["LONG", "short", "sideways", ""])
def test_condition_rejects_directions_outside_the_bias_vocabulary(direction):
    with pytest.raises(ValidationError):
        Condition(type=ConditionType.FVG, direction=direction)


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [("bullish", "bullish"), ("bearish", "bearish"), ("BULLISH", "bullish"), (" Bearish ", "bearish"), (None, None)],
)
def test_valid_bias_directions_are_accepted_and_normalized(supplied, expected):
    # Case and surrounding whitespace are normalized rather than rejected,
    # so the engines' `direction.lower() == "bullish"` reads a canonical
    # value and `None` still means "either direction, take the SMC bias".
    assert StrategyDefinition(name="S", market="X", timeframe="15m", direction=supplied).direction == expected
    assert Condition(type=ConditionType.FVG, direction=supplied).direction == expected
