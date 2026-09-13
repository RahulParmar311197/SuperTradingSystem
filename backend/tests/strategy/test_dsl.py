import pytest
from pydantic import ValidationError

from app.strategy.dsl import Condition, ConditionOperator, ConditionType, EntryConfig, StrategyDefinition


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
    # A live condition type, so this proves operator rejection alone --
    # `ConditionType.INDICATOR` is now rejected on its own account by
    # `_reject_unfed_condition_types`, which would mask what this asserts.
    with pytest.raises(ValidationError, match="not yet implemented"):
        Condition(type=ConditionType.FVG, direction="bullish", operator=operator, min_value=40, max_value=60)


@pytest.mark.parametrize(
    "operator", [ConditionOperator.GREATER_THAN, ConditionOperator.LESS_THAN, ConditionOperator.WITHIN]
)
def test_condition_accepts_implemented_operators(operator):
    condition = Condition(
        type=ConditionType.FVG, direction="bullish", operator=operator, value=50, min_value=40, max_value=60
    )
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


@pytest.mark.parametrize(
    "entry_type", ["limit", "fvg-retest", "retest", "fvg_retest_entry", "market_order", "", "  "]
)
def test_entry_config_rejects_entry_types_the_engine_cannot_resolve(entry_type):
    # Regression test: `EntryConfig.type` was a bare `str` and
    # `app/strategy/engine.py`'s `_resolve_entry_and_stop` read it as a
    # chain of bare equality tests with an implicit `else` -- anything
    # that was not exactly "fvg_retest" or "order_block_retest" fell
    # through to a *market* entry at `context.current_price`, stopped at
    # the dealing-range edge. That is a different trade, not a near miss:
    # on the same candles a 'fvg_retest' strategy waits for price to come
    # back to the gap, while its silent market substitute chases the top
    # of the move with a stop 2.5x further away, and fills on a candle
    # where the strategy as written would not have traded at all.
    #
    # This is a live path: POST /strategies, PUT /strategies/{id} and
    # app.ai.strategy_builder.parse_strategy_json all validate through
    # this model, and the AI is explicitly told it may use "entry types
    # the schema defines" -- which, until now, the schema did not define.
    with pytest.raises(ValidationError):
        EntryConfig(type=entry_type)
    with pytest.raises(ValidationError):
        StrategyDefinition(name="S", market="X", timeframe="15m", entry={"type": entry_type})


@pytest.mark.parametrize(
    ("supplied", "expected"),
    [
        ("market", "market"),
        ("fvg_retest", "fvg_retest"),
        ("order_block_retest", "order_block_retest"),
        ("FVG_RETEST", "fvg_retest"),
        (" Order_Block_Retest ", "order_block_retest"),
    ],
)
def test_valid_entry_types_are_accepted_and_normalized(supplied, expected):
    # Case and surrounding whitespace are normalized rather than rejected,
    # matching `_validate_bias`, so `_resolve_entry_and_stop`'s
    # `entry_type == "fvg_retest"` reads a canonical value instead of
    # silently falling through to a market entry.
    assert EntryConfig(type=supplied).type == expected
    assert StrategyDefinition(name="S", market="X", timeframe="15m", entry={"type": supplied}).entry.type == expected


def test_the_default_entry_type_is_still_a_market_entry():
    assert EntryConfig().type == "market"
    assert StrategyDefinition(name="S", market="X", timeframe="15m").entry.type == "market"


# --- premium_discount needs the zone it compares against -------------------


def test_premium_discount_without_a_zone_is_rejected():
    """A `premium_discount` condition with no `zone` can never match.

    Its evaluator arm ends in `condition.zone is not None and ...`, so the
    zone-less form is false on every candle forever. Measured against a
    live dealing range with `current_zone='PREMIUM'`: zone-less returned
    False, `zone='premium'` returned True.

    Conditions AND implicitly, so one of these zeroes the whole strategy --
    which stores, lists and backtests like any other and simply never
    produces a signal. Same ruling as the unfed condition types and the
    unresolvable entry types above.
    """
    with pytest.raises(ValidationError) as excinfo:
        Condition(type=ConditionType.PREMIUM_DISCOUNT)
    assert "needs a zone" in str(excinfo.value)


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_zone_is_rejected_too(blank):
    """Control on the boundary: an empty or whitespace string is the same
    unusable state as the field being absent, and `.strip()` is what makes
    them agree."""
    with pytest.raises(ValidationError):
        Condition(type=ConditionType.PREMIUM_DISCOUNT, zone=blank)


@pytest.mark.parametrize("zone", ["premium", "discount", "equilibrium", "PREMIUM"])
def test_a_stated_zone_is_accepted(zone):
    """Control: this validator refuses an absent zone, not a zone it
    dislikes. Case is left as written -- the evaluator lowercases both
    sides when it compares."""
    assert Condition(type=ConditionType.PREMIUM_DISCOUNT, zone=zone).zone == zone


def test_an_unrecognised_zone_is_still_accepted():
    """Control on the scope, and deliberately so.

    A misspelled zone also never matches, but `_validate_bias`'s docstring
    records leaving free-text `side`/`zone` values to fail closed as an
    intentional decision. This validator fills the gap that decision does
    not cover -- a value the author never supplied -- and does not overturn
    it. If that changes, this test is the one that should be rewritten
    first, on purpose rather than by accident.
    """
    assert Condition(type=ConditionType.PREMIUM_DISCOUNT, zone="premuim").zone == "premuim"


@pytest.mark.parametrize(
    "condition_type", [ConditionType.FVG, ConditionType.ORDER_BLOCK, ConditionType.TREND, ConditionType.SESSION]
)
def test_the_other_types_still_treat_an_omitted_optional_as_any(condition_type):
    """Control: the asymmetry this fixes was real, and only in one arm.
    Every other type reads an omitted optional as "any" and must keep
    constructing without one."""
    assert Condition(type=condition_type).type is condition_type
