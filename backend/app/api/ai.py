import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import AIUnavailableError, get_ai_client
from app.ai.context_builder import build_ai_prompt_context
from app.ai.explanation import build_trade_explanation
from app.ai.strategy_builder import StrategyBuilderError, build_strategy_from_description
from app.ai.validation import validate_ai_trade_proposal
from app.auth.dependencies import get_current_user
from app.core.config import get_settings
from app.database.models.ai import AIDecision, AIDecisionType
from app.database.models.instruments import Instrument
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.users import User
from app.database.session import get_db
from app.ict.engine import ICTConfig, ICTEngine
from app.market.repository import get_candles
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import StrategyDefinition
from app.strategy.engine import StrategyEngine

router = APIRouter(prefix="/ai", tags=["ai"])


async def _build_context(db: AsyncSession, instrument_id: uuid.UUID, timeframe: str) -> EvaluationContext:
    candles = await get_candles(db, instrument_id, timeframe)
    if len(candles) < 3:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Not enough candle history for analysis")
    return EvaluationContext(
        symbol=str(instrument_id),
        timeframe=timeframe,
        timestamp=candles[-1].timestamp,
        current_price=candles[-1].close,
        smc=SMCEngine(SMCConfig()).analyze(candles),
        ict=ICTEngine(ICTConfig()).analyze(candles),
    )


class AnalyzeRequest(BaseModel):
    instrument_id: uuid.UUID
    timeframe: str


@router.post("/analyze")
async def analyze(
    payload: AnalyzeRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    context = await _build_context(db, payload.instrument_id, payload.timeframe)
    return build_ai_prompt_context(context)


class BuildStrategyRequest(BaseModel):
    description: str
    market: str
    timeframe: str


@router.post("/strategy")
async def build_strategy_endpoint(
    payload: BuildStrategyRequest, user: User = Depends(get_current_user)
) -> StrategyDefinition:
    settings = get_settings()
    ai_client = get_ai_client(settings)
    try:
        return await build_strategy_from_description(payload.description, payload.market, payload.timeframe, ai_client)
    except AIUnavailableError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except ValueError as exc:
        # The AI responded but its content wasn't usable (bad JSON, or JSON
        # that fails the Strategy DSL schema) — not our fault, not the
        # caller's; a bad gateway to the upstream AI provider.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except StrategyBuilderError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


class ExplainTradeRequest(BaseModel):
    strategy_id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str


@router.post("/explain-trade")
async def explain_trade(
    payload: ExplainTradeRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    strategy_row = await db.get(StrategyRow, payload.strategy_id)
    if strategy_row is None or strategy_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    strategy = StrategyDefinition.model_validate(strategy_row.definition)

    context = await _build_context(db, payload.instrument_id, payload.timeframe)
    result = StrategyEngine().evaluate(strategy, context)
    explanation = build_trade_explanation(context, result)
    return explanation.__dict__


_TRADE_PROPOSAL_SYSTEM_PROMPT = (
    "You review a trading setup's structured facts and either confirm or decline a trade. "
    'Respond with JSON only, in exactly this shape: {"decision": "TRADE" or "NO_TRADE", '
    '"direction": "bullish" or "bearish", "entry": number, "stop": number, "risk_reward": number, '
    '"risk_percent": number, "reasoning": string}. Every number must match the structured facts '
    "you were given — you are not authorized to invent your own entry, stop, or risk/reward."
)


class ProposeTradeRequest(BaseModel):
    strategy_id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str
    max_risk_percent: float = 1.0


class ProposeTradeResponse(BaseModel):
    decision_id: uuid.UUID
    valid: bool
    errors: list[str]
    proposal: dict
    deterministic: dict


@router.post("/propose-trade", response_model=ProposeTradeResponse)
async def propose_trade(
    payload: ProposeTradeRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ProposeTradeResponse:
    """Blueprint §80-81 "AI Prompt Architecture" / "AI Output Validation",
    and §87's "Assisted" mode: the AI proposes a trade for an already-
    detected setup, every number it states is cross-checked against the
    deterministic `StrategyEngine` result (`app.ai.validation`, blueprint
    §131 "AI ≠ Final Authority"), and the outcome is persisted as an
    `AIDecision` row (blueprint §71 audit logging, §79 "AI Model
    Evaluation" — you can't evaluate AI behavior over time without a
    record of what it actually said). This never places an order itself;
    a validated proposal still has to go through `POST /orders` like any
    other trade, with the deterministic risk engine as the final gate.
    """
    strategy_row = await db.get(StrategyRow, payload.strategy_id)
    if strategy_row is None or strategy_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    strategy = StrategyDefinition.model_validate(strategy_row.definition)

    instrument = await db.get(Instrument, payload.instrument_id)
    if instrument is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Instrument not found")

    context = await _build_context(db, payload.instrument_id, payload.timeframe)
    deterministic_result = StrategyEngine().evaluate(strategy, context)
    prompt_context = build_ai_prompt_context(context)
    deterministic_summary = {
        "matched": deterministic_result.matched,
        "direction": deterministic_result.direction,
        "entry": deterministic_result.entry,
        "stop": deterministic_result.stop,
        "target": deterministic_result.target,
        "risk_reward": deterministic_result.risk_reward,
    }
    input_context = {
        "strategy_name": strategy.name,
        "prompt_context": prompt_context,
        "max_risk_percent": payload.max_risk_percent,
    }

    settings = get_settings()
    ai_client = get_ai_client(settings)
    prompt = (
        f"Strategy: {strategy.name}\nStructured facts: {json.dumps(prompt_context, default=str)}\n\n"
        "Respond with the JSON proposal only."
    )

    try:
        proposal = await ai_client.complete_json(prompt, system=_TRADE_PROPOSAL_SYSTEM_PROMPT)
    except AIUnavailableError as exc:
        # Blueprint §110 "no AI -> no trade": still recorded, so an
        # attempted-but-blocked proposal is auditable like any other.
        db.add(
            AIDecision(
                user_id=user.id,
                decision_type=AIDecisionType.TRADE_PROPOSAL,
                input_context=input_context,
                output={"error": str(exc)},
                validated=False,
                validation_errors=["AI unavailable"],
                model=None,
            )
        )
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    validation = validate_ai_trade_proposal(
        proposal,
        deterministic_result,
        instrument_tradable=instrument.active,
        max_risk_percent=payload.max_risk_percent,
    )

    decision = AIDecision(
        user_id=user.id,
        decision_type=AIDecisionType.TRADE_PROPOSAL,
        input_context=input_context,
        output=proposal,
        validated=validation.valid,
        validation_errors=validation.errors,
        model=settings.ai_model,
    )
    db.add(decision)
    await db.commit()
    await db.refresh(decision)

    return ProposeTradeResponse(
        decision_id=decision.id,
        valid=validation.valid,
        errors=validation.errors,
        proposal=proposal,
        deterministic=deterministic_summary,
    )
