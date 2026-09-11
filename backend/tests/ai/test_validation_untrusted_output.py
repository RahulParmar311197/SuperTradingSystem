"""`validate_ai_trade_proposal` against AI responses that are valid JSON
but not the shape the system prompt asked for.

Regression tests. The validator used to call `float()` straight on
whatever the model put in `entry`/`stop`/`risk_reward`/`risk_percent` and
`.get()` straight on the response itself, so an `entry` of `"N/A"` raised
`ValueError` and a top-level JSON array raised `AttributeError` — out of
the validator, out of `POST /ai/propose-trade` (which calls this *before*
writing its `AIDecision` audit row), and into a bare 500 with no record of
what the AI said. Every existing test fed it a well-formed dict of real
floats, so nothing here was ever exercised.

It also ignored `decision` entirely — the one field in the response that
carries the model's own verdict — so an AI that answered "NO_TRADE" while
echoing the deterministic numbers it was instructed to match came back
`valid: true`.
"""

import pytest

from app.ai.validation import validate_ai_trade_proposal
from app.strategy.engine import StrategyEvaluationResult


def _matched_result() -> StrategyEvaluationResult:
    return StrategyEvaluationResult(
        matched=True,
        satisfied=["fvg"],
        missing=[],
        direction="bullish",
        entry=100.0,
        stop=98.0,
        target=104.0,
        risk_reward=2.0,
        score=75.0,
    )


def _proposal(**overrides) -> dict:
    base = {
        "decision": "TRADE",
        "direction": "bullish",
        "entry": 100.0,
        "stop": 98.0,
        "risk_reward": 2.0,
        "risk_percent": 0.5,
    }
    base.update(overrides)
    return base


def _validate(proposal):
    return validate_ai_trade_proposal(proposal, _matched_result(), instrument_tradable=True, max_risk_percent=1.0)


# --- the response isn't a JSON object at all -------------------------------


@pytest.mark.parametrize(
    "proposal",
    [
        ["NO_TRADE"],
        "NO_TRADE",
        42,
        None,
        [{"decision": "TRADE", "entry": 100.0}],
    ],
)
def test_a_response_that_is_not_a_json_object_is_a_validation_failure(proposal):
    result = _validate(proposal)
    assert result.valid is False
    assert result.errors, "a non-object response must say why it was rejected"


# --- a field that isn't a number -------------------------------------------


@pytest.mark.parametrize("key", ["entry", "stop", "risk_reward", "risk_percent"])
@pytest.mark.parametrize("value", ["N/A", "1,250.50", "2:1", "0.5%", "", {"value": 100.0}, [100.0], True, False])
def test_a_non_numeric_field_is_a_validation_failure_not_an_exception(key, value):
    result = _validate(_proposal(**{key: value}))
    assert result.valid is False
    assert any(key in e for e in result.errors), result.errors


@pytest.mark.parametrize("value", ["inf", "-inf", "nan", "Infinity", float("inf"), float("nan")])
def test_a_non_finite_entry_is_a_validation_failure(value):
    result = _validate(_proposal(entry=value))
    assert result.valid is False
    assert any("entry" in e for e in result.errors), result.errors


def test_a_missing_field_is_a_validation_failure():
    proposal = _proposal()
    del proposal["stop"]
    result = _validate(proposal)
    assert result.valid is False
    assert any("stop" in e for e in result.errors), result.errors


def test_a_numeric_string_is_still_accepted():
    # The coercion must not be so strict it rejects a model that quoted
    # its numbers -- "100.1" is a number, "1,250.50" is not.
    result = _validate(_proposal(entry="100.1", stop="98", risk_reward="2.0", risk_percent="0.5"))
    assert result.valid is True, result.errors


# --- the AI's own verdict ---------------------------------------------------


def test_a_declined_trade_is_not_valid_even_when_every_number_matches():
    result = _validate(_proposal(decision="NO_TRADE"))
    assert result.valid is False
    assert any("NO_TRADE" in e for e in result.errors), result.errors


def test_a_response_with_no_decision_at_all_is_not_valid():
    proposal = _proposal()
    del proposal["decision"]
    result = _validate(proposal)
    assert result.valid is False


@pytest.mark.parametrize("decision", ["TRADE", "trade", " Trade "])
def test_a_trade_verdict_is_accepted_regardless_of_formatting(decision):
    result = _validate(_proposal(decision=decision))
    assert result.valid is True, result.errors


@pytest.mark.parametrize("decision", ["MAYBE", "", None, 1, ["TRADE"]])
def test_any_other_verdict_is_a_validation_failure(decision):
    result = _validate(_proposal(decision=decision))
    assert result.valid is False


# --- the happy path still works --------------------------------------------


def test_a_well_formed_endorsed_proposal_is_still_valid():
    result = _validate(_proposal())
    assert result.valid is True
    assert result.errors == []
