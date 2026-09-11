"""Condition types nothing feeds must be refused, not silently never matched.

`app/strategy/evaluator.py` routes six of the fifteen `ConditionType` members
-- volume, volatility, indicator, options_iv, options_oi, options_greeks --
to `_numeric_compare(context.indicators.get(key), condition)`. But
`EvaluationContext.indicators` has no writer anywhere in `app/`: all five
construction sites (`backtest/engine.py`, `paper/engine.py`, `api/ai.py`,
`api/scanner.py`, `workers/scanner_worker.py`) omit it, so the bag is always
empty, `.get` always returns `None`, and `_numeric_compare` returns `False` on
its first line.

Because conditions AND implicitly, one such condition zeroes the whole
strategy: a working `fvg` strategy stops producing signals the moment a
*vacuously true* `volume > 0` is added, against bars that carry volume.
Nothing surfaces why -- `POST /backtest` runs the same evaluator over the same
context shape, so a validation backtest reports zero trades, which is
indistinguishable from "this history had no setups", and blueprint §77's
graduation path cannot catch it at any stage.

This mirrors the rulings already made for AND/OR/NOT operators
(`_reject_unimplemented_boolean_operators`) and for unknown entry types
(`_reject_unknown_entry_types`).

Why the existing tests missed it:
`tests/strategy/test_dsl.py::test_condition_accepts_implemented_operators` is
the test that *looks* like coverage -- it builds a `ConditionType.INDICATOR`
condition -- but it only asserts `condition.operator == operator`, both sides
of which come from the same parametrised value (shape a), and it never
evaluates the condition. Both shared context helpers
(`test_engine.py::_build_context`, `test_evaluator.py::_context`) construct
`EvaluationContext` without `indicators`, exactly like the production sites,
so no fixture in the suite can produce a non-empty bag (shape b). And the six
types had no evaluation test of any kind, nor did `_numeric_compare` (shape f).
"""

import pytest
from pydantic import ValidationError

from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType, StrategyDefinition
from app.strategy.scoring import DEFAULT_WEIGHTS, compute_strategy_score

# Stated here rather than imported from the module under test: reading the
# answer out of the code would make both sides of every assertion below come
# from the same value, and it keeps this readable as a specification of which
# six types have no data source.
UNFED = [
    ConditionType.INDICATOR,
    ConditionType.OPTIONS_GREEKS,
    ConditionType.OPTIONS_IV,
    ConditionType.OPTIONS_OI,
    ConditionType.VOLATILITY,
    ConditionType.VOLUME,
]
FED = [t for t in ConditionType if t not in UNFED]


def _definition(conditions: list[dict]) -> dict:
    return {
        "name": "s",
        "market": "TESTSYM",
        "timeframe": "15m",
        "direction": "bullish",
        "conditions": conditions,
        "entry": {"type": "fvg_retest"},
        "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
    }


@pytest.mark.parametrize("condition_type", UNFED, ids=lambda t: t.value)
def test_a_condition_type_nothing_feeds_is_refused(condition_type):
    # Regression test: these validated, persisted, and were then false on
    # every candle forever.
    with pytest.raises(ValidationError, match="silently never match"):
        Condition(type=condition_type, name="rsi", operator="GREATER_THAN", value=1.0)


@pytest.mark.parametrize("condition_type", UNFED, ids=lambda t: t.value)
def test_a_whole_strategy_carrying_one_is_refused(condition_type):
    # The shape that actually reached the database: `POST /strategies` takes a
    # `StrategyDefinition` as its body and persists `model_dump(mode="json")`,
    # so a definition that validates is a definition that is stored.
    with pytest.raises(ValidationError, match="silently never match"):
        StrategyDefinition.model_validate(
            _definition(
                [
                    {"type": "fvg", "direction": "bullish"},
                    {"type": condition_type.value, "operator": "GREATER_THAN", "value": 0.0},
                ]
            )
        )


def test_the_rejection_names_the_types_that_do_work():
    # The message has to be actionable: someone reaching for `volume` needs to
    # know what to reach for instead.
    with pytest.raises(ValidationError) as excinfo:
        Condition(type=ConditionType.VOLUME, operator="GREATER_THAN", value=0.0)

    message = str(excinfo.value)
    assert "fvg" in message and "liquidity_sweep" in message
    for condition_type in UNFED:
        assert f"({condition_type.value}" not in message, "an unfed type must not be suggested"


@pytest.mark.parametrize("condition_type", FED, ids=lambda t: t.value)
def test_every_remaining_condition_type_still_validates(condition_type):
    # The control: this rejects six types, not the DSL. Each survivor must
    # still build, since each is read from data the context really carries.
    condition = Condition(type=condition_type, direction="bullish", name="LONDON", zone="premium", side="buy")
    assert condition.type is condition_type


def test_the_indicators_bag_still_has_no_writer():
    # The property this validator exists for. If a real writer is ever added,
    # this fails and the validator should be narrowed to whatever is still
    # unfed rather than left blocking types that now work.
    context = EvaluationContext(
        symbol="TESTSYM", timeframe="15m", timestamp=None, current_price=100.0,
        smc=None, ict=None, current_index=0,
    )
    assert context.indicators == {}


def test_the_score_denominator_matches_what_can_actually_be_earned():
    # `ConditionType.VOLATILITY` can never appear in `satisfied_condition_types`
    # now, so a "volatility" weight would be permanently unearnable: the total
    # was 100 while the reachable ceiling was 90, and nothing could score
    # above 90.
    assert "volatility" not in DEFAULT_WEIGHTS

    context = EvaluationContext(
        symbol="TESTSYM", timeframe="15m", timestamp=None, current_price=100.0,
        smc=None, ict=None, current_index=0,
    )
    everything_reachable = [
        ConditionType.TREND, ConditionType.BOS, ConditionType.LIQUIDITY_SWEEP, ConditionType.FVG
    ]
    assert compute_strategy_score(context, everything_reachable, risk_reward=3.0, minimum_rr=2.0) == 100.0


def test_scoring_still_ranks_a_stronger_setup_above_a_weaker_one():
    # The control on that rescale: dropping one unearnable component divides
    # every score by the same smaller total, so ordering is untouched.
    context = EvaluationContext(
        symbol="TESTSYM", timeframe="15m", timestamp=None, current_price=100.0,
        smc=None, ict=None, current_index=0,
    )
    weaker = compute_strategy_score(context, [ConditionType.TREND], risk_reward=3.0, minimum_rr=2.0)
    stronger = compute_strategy_score(
        context, [ConditionType.TREND, ConditionType.BOS, ConditionType.FVG], risk_reward=3.0, minimum_rr=2.0
    )
    assert 0 < weaker < stronger <= 100.0
