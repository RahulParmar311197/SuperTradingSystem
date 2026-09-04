import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database.models.strategy import Signal as SignalRow
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.users import User
from app.database.session import get_db
from app.ict.engine import ICTConfig, ICTEngine
from app.market.repository import get_candles
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import StrategyDefinition
from app.strategy.engine import StrategyEngine

router = APIRouter(tags=["scanner"])


class ScannerRequest(BaseModel):
    strategy_id: uuid.UUID
    instrument_ids: list[uuid.UUID]
    timeframe: str


class ScannerResult(BaseModel):
    instrument_id: uuid.UUID
    matched: bool
    score: float
    direction: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_reward: float | None = None


@router.post("/scanner", response_model=list[ScannerResult])
async def run_scanner(
    payload: ScannerRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ScannerResult]:
    strategy_row = await db.get(StrategyRow, payload.strategy_id)
    if strategy_row is None or strategy_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    strategy = StrategyDefinition.model_validate(strategy_row.definition)

    smc_engine = SMCEngine(SMCConfig())
    ict_engine = ICTEngine(ICTConfig())
    strategy_engine = StrategyEngine()

    results: list[ScannerResult] = []
    for instrument_id in payload.instrument_ids:
        candles = await get_candles(db, instrument_id, payload.timeframe)
        if len(candles) < 3:
            results.append(ScannerResult(instrument_id=instrument_id, matched=False, score=0.0))
            continue

        context = EvaluationContext(
            symbol=str(instrument_id),
            timeframe=payload.timeframe,
            timestamp=candles[-1].timestamp,
            current_price=candles[-1].close,
            smc=smc_engine.analyze(candles),
            ict=ict_engine.analyze(candles),
        )
        outcome = strategy_engine.evaluate(strategy, context)
        results.append(
            ScannerResult(
                instrument_id=instrument_id,
                matched=outcome.matched,
                score=outcome.score,
                direction=outcome.direction,
                entry=outcome.entry,
                stop=outcome.stop,
                target=outcome.target,
                risk_reward=outcome.risk_reward,
            )
        )

    results.sort(key=lambda r: r.score, reverse=True)
    return results


class SignalResponse(BaseModel):
    id: uuid.UUID
    instrument_id: uuid.UUID
    timeframe: str
    direction: str
    entry: float
    stop: float
    target: float
    risk_reward: float
    score: float
    generated_at: datetime

    model_config = {"from_attributes": True}


@router.get("/signals", response_model=list[SignalResponse])
async def list_signals(
    instrument_id: uuid.UUID | None = None, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[SignalRow]:
    stmt = select(SignalRow).order_by(SignalRow.generated_at.desc()).limit(200)
    if instrument_id is not None:
        stmt = stmt.where(SignalRow.instrument_id == instrument_id)
    return (await db.execute(stmt)).scalars().all()
