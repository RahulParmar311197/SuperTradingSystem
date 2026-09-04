import pytest

from app.ai.strategy_builder import StrategyBuilderError, parse_strategy_json
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


def test_valid_proposal_matching_deterministic_result_passes():
    proposal = {"direction": "bullish", "entry": 100.1, "stop": 98.0, "risk_reward": 2.0, "risk_percent": 0.5}
    result = validate_ai_trade_proposal(proposal, _matched_result(), instrument_tradable=True, max_risk_percent=1.0)
    assert result.valid is True
    assert result.errors == []


def test_rejects_when_no_signal_exists():
    no_signal = StrategyEvaluationResult(matched=False, satisfied=[], missing=["fvg"])
    result = validate_ai_trade_proposal(
        {"direction": "bullish", "entry": 100, "stop": 98, "risk_reward": 2, "risk_percent": 0.5},
        no_signal,
        instrument_tradable=True,
        max_risk_percent=1.0,
    )
    assert result.valid is False
    assert "No signal exists" in result.errors[0]


def test_rejects_hallucinated_entry_price():
    proposal = {"direction": "bullish", "entry": 150.0, "stop": 98.0, "risk_reward": 2.0, "risk_percent": 0.5}
    result = validate_ai_trade_proposal(proposal, _matched_result(), instrument_tradable=True, max_risk_percent=1.0)
    assert result.valid is False
    assert any("entry" in e for e in result.errors)


def test_rejects_excessive_risk_percent():
    proposal = {"direction": "bullish", "entry": 100.0, "stop": 98.0, "risk_reward": 2.0, "risk_percent": 5.0}
    result = validate_ai_trade_proposal(proposal, _matched_result(), instrument_tradable=True, max_risk_percent=1.0)
    assert result.valid is False
    assert any("risk_percent" in e for e in result.errors)


def test_rejects_untradable_instrument():
    proposal = {"direction": "bullish", "entry": 100.0, "stop": 98.0, "risk_reward": 2.0, "risk_percent": 0.5}
    result = validate_ai_trade_proposal(proposal, _matched_result(), instrument_tradable=False, max_risk_percent=1.0)
    assert result.valid is False
    assert any("not currently tradable" in e for e in result.errors)


def test_parse_strategy_json_accepts_valid_schema():
    raw = {
        "name": "Bullish Liquidity Sweep",
        "market": "NIFTY",
        "timeframe": "15m",
        "conditions": [{"type": "fvg", "direction": "bullish"}],
        "entry": {"type": "fvg_retest"},
        "risk": {"risk_percent": 0.5, "minimum_rr": 2},
    }
    strategy = parse_strategy_json(raw)
    assert strategy.name == "Bullish Liquidity Sweep"


def test_parse_strategy_json_rejects_invalid_condition_type():
    raw = {
        "name": "Bad",
        "market": "NIFTY",
        "timeframe": "15m",
        "conditions": [{"type": "not_a_real_condition"}],
    }
    with pytest.raises(StrategyBuilderError):
        parse_strategy_json(raw)


@pytest.mark.asyncio
async def test_null_ai_client_raises_unavailable_error():
    from app.ai.client import AIUnavailableError, NullAIClient

    with pytest.raises(AIUnavailableError):
        await NullAIClient().complete_json("prompt")
