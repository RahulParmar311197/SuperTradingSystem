import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.client import AIUnavailableError, get_ai_client
from app.ai.context_builder import build_ai_prompt_context
from app.ai.explanation import build_trade_explanation
from app.ai.strategy_builder import StrategyBuilderError, build_strategy_from_description
from app.auth.dependencies import get_current_user
from app.core.config import get_settings
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
