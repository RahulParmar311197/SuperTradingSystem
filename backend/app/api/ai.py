import dataclasses
import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import AIProviderError, AIUnavailableError, get_ai_client
from app.ai.context_builder import build_ai_prompt_context
from app.ai.explanation import build_trade_explanation
from app.ai.strategy_builder import StrategyBuilderError, build_strategy_from_description
from app.ai.validation import validate_ai_trade_proposal
from app.auth.dependencies import get_current_user
from app.core.config import get_settings
from app.database.models.ai import AIDecision, AIDecisionType, AIMessage
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
        current_index=len(candles) - 1,
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
    except (AIProviderError, ValueError) as exc:
        # A configured provider's API call itself failing (AIProviderError
        # -- rate limit, timeout, connection error) or responding with
        # content that wasn't usable (bad JSON, or JSON that fails the
        # Strategy DSL schema -- both surface as ValueError) are both
        # equally "not our fault, not the caller's" -- a bad gateway to
        # the upstream AI provider.
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
    # TradeExplanation is `@dataclass(slots=True)` — no `__dict__` attribute;
    # this endpoint had never had a test hit it, so it 500'd on every call.
    return dataclasses.asdict(explanation)


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
    except (AIUnavailableError, AIProviderError, ValueError) as exc:
        # Blueprint §110 "no AI -> no trade": still recorded, so an
        # attempted-but-failed proposal is auditable like any other --
        # whether no provider is configured at all (`AIUnavailableError`),
        # a configured provider's API call itself failed (`AIProviderError`
        # -- rate limit, timeout, connection error), or it answered with
        # content that wasn't usable (`ValueError`/`AIResponseParseError`).
        # The latter two are the far more likely failure modes in a real
        # deployment (they only fire once a provider *is* configured), and
        # before this they propagated straight past this endpoint's own
        # audit guarantee to a bare 500 with no `AIDecision` row at all.
        if isinstance(exc, AIUnavailableError):
            status_code, reason = status.HTTP_503_SERVICE_UNAVAILABLE, "AI unavailable"
        elif isinstance(exc, AIProviderError):
            status_code, reason = status.HTTP_502_BAD_GATEWAY, "AI provider error"
        else:
            status_code, reason = status.HTTP_502_BAD_GATEWAY, "AI response unusable"
        db.add(
            AIDecision(
                user_id=user.id,
                decision_type=AIDecisionType.TRADE_PROPOSAL,
                input_context=input_context,
                output={"error": str(exc)},
                validated=False,
                validation_errors=[reason],
                model=None,
            )
        )
        await db.commit()
        raise HTTPException(status_code, str(exc)) from exc

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


_CHAT_SYSTEM_PROMPT = (
    "You are a trading assistant embedded in this platform (blueprint §96 'AI Screen'). "
    "Answer the user's question about markets, setups, or strategies. If 'structured facts' are "
    "provided, ground your answer only in those facts -- never invent a price, level, or indicator "
    "value that isn't in them. If no facts are provided and the question needs specific market data "
    "you don't have, say so plainly instead of guessing. "
    'Respond with JSON only, in exactly this shape: {"reply": string}.'
)


class ChatRequest(BaseModel):
    message: str
    instrument_id: uuid.UUID | None = None
    timeframe: str | None = None


class ChatMessageResponse(BaseModel):
    id: uuid.UUID
    role: str
    content: str
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/chat", response_model=ChatMessageResponse)
async def chat(
    payload: ChatRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> AIMessage:
    """Blueprint §96 "AI Screen": a single-turn, grounded Q&A endpoint --
    not a full intent router across every other AI feature in this file.
    Each call is independent (no prior turns are fed back as context);
    `GET /ai/chat/history` exists so a frontend can render the
    conversation, not so this endpoint can "remember" it.
    """
    db.add(AIMessage(user_id=user.id, role="user", content=payload.message))

    prompt_context: dict | None = None
    if payload.instrument_id is not None and payload.timeframe is not None:
        context = await _build_context(db, payload.instrument_id, payload.timeframe)
        prompt_context = build_ai_prompt_context(context)

    prompt = payload.message
    if prompt_context is not None:
        prompt = f"Structured facts: {json.dumps(prompt_context, default=str)}\n\nQuestion: {payload.message}"

    settings = get_settings()
    ai_client = get_ai_client(settings)
    try:
        response = await ai_client.complete_json(prompt, system=_CHAT_SYSTEM_PROMPT)
        reply = str(response.get("reply", "")) if isinstance(response, dict) else str(response)
    except (AIUnavailableError, AIProviderError, ValueError) as exc:
        # Same fix as propose_trade above: a configured provider's API
        # call failing (AIProviderError) or answering with unusable
        # content (ValueError/AIResponseParseError) used to propagate past
        # this except clause entirely -- skipping the assistant AIMessage
        # row below, and since the earlier `db.add(AIMessage(role="user",
        # ...))` was never committed before the exception, the user's own
        # message vanished from `GET /ai/chat/history` too, with no
        # visible reply anywhere.
        if isinstance(exc, AIUnavailableError):
            status_code, prefix = status.HTTP_503_SERVICE_UNAVAILABLE, "AI unavailable"
        else:
            status_code, prefix = status.HTTP_502_BAD_GATEWAY, "AI error"
        assistant_message = AIMessage(user_id=user.id, role="assistant", content=f"{prefix}: {exc}")
        db.add(assistant_message)
        await db.commit()
        raise HTTPException(status_code, str(exc)) from exc

    assistant_message = AIMessage(user_id=user.id, role="assistant", content=reply)
    db.add(assistant_message)
    await db.commit()
    await db.refresh(assistant_message)
    return assistant_message


@router.get("/chat/history", response_model=list[ChatMessageResponse])
async def chat_history(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db), limit: int = 50
) -> list[AIMessage]:
    stmt = select(AIMessage).where(AIMessage.user_id == user.id).order_by(AIMessage.created_at).limit(limit)
    return (await db.execute(stmt)).scalars().all()
