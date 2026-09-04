import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.users import User
from app.database.session import get_db
from app.paper.engine import PaperTradingEngine
from app.smc.types import Candle
from app.strategy.dsl import StrategyDefinition

router = APIRouter(prefix="/paper", tags=["paper"])

# In-memory session registry — see the note in app/api/replay.py.
_SESSIONS: dict[uuid.UUID, PaperTradingEngine] = {}


def _get_session(session_id: uuid.UUID) -> PaperTradingEngine:
    engine = _SESSIONS.get(session_id)
    if engine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper trading session not found")
    return engine


class CreatePaperSessionRequest(BaseModel):
    strategy_id: uuid.UUID
    symbol: str
    starting_balance: float = 100_000.0


class PaperStateResponse(BaseModel):
    session_id: uuid.UUID
    balance: float
    equity: float
    open_position: dict | None
    trades_today: int


async def _state_response(session_id: uuid.UUID, engine: PaperTradingEngine) -> PaperStateResponse:
    account = await engine.broker.get_account()
    position = engine.position_manager.get(engine.account_id, engine.symbol)
    return PaperStateResponse(
        session_id=session_id,
        balance=account.balance,
        equity=account.equity,
        open_position=(
            {
                "quantity": position.quantity,
                "average_price": position.average_price,
                "unrealized_pnl": position.unrealized_pnl,
                "stop": position.stop,
                "target": position.target,
            }
            if position and position.is_open
            else None
        ),
        trades_today=engine.trades_today,
    )


@router.post("", response_model=PaperStateResponse)
async def create_paper_session(
    payload: CreatePaperSessionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaperStateResponse:
    strategy_row = await db.get(StrategyRow, payload.strategy_id)
    if strategy_row is None or strategy_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    strategy = StrategyDefinition.model_validate(strategy_row.definition)

    session_id = uuid.uuid4()
    engine = PaperTradingEngine(
        strategy, symbol=payload.symbol, account_id=str(user.id), starting_balance=payload.starting_balance
    )
    _SESSIONS[session_id] = engine
    return await _state_response(session_id, engine)


@router.get("/{session_id}", response_model=PaperStateResponse)
async def get_paper_session(session_id: uuid.UUID, user: User = Depends(get_current_user)) -> PaperStateResponse:
    return await _state_response(session_id, _get_session(session_id))


class FeedCandleRequest(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@router.post("/{session_id}/candle", response_model=PaperStateResponse)
async def feed_candle(
    session_id: uuid.UUID, payload: FeedCandleRequest, user: User = Depends(get_current_user)
) -> PaperStateResponse:
    engine = _get_session(session_id)
    await engine.on_candle(Candle(**payload.model_dump()))
    return await _state_response(session_id, engine)
