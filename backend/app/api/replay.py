import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database.models.users import User
from app.database.session import get_db
from app.market.repository import get_candles
from app.replay.engine import ReplayEngine, ReplayError
from app.smc.engine import SMCConfig

router = APIRouter(prefix="/replay", tags=["replay"])

# In-memory session registry — a single-process dev/demo store. Promote to
# Redis-backed sessions (blueprint §65) before running multiple API workers.
_SESSIONS: dict[uuid.UUID, ReplayEngine] = {}


def _get_session(session_id: uuid.UUID) -> ReplayEngine:
    engine = _SESSIONS.get(session_id)
    if engine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Replay session not found")
    return engine


class CreateReplayRequest(BaseModel):
    instrument_id: uuid.UUID
    timeframe: str
    starting_balance: float = 100_000.0
    swing_length: int = 3


class ReplayStateResponse(BaseModel):
    session_id: uuid.UUID
    cursor: int
    total_candles: int
    status: str
    balance: float
    current_price: float
    open_trade: dict | None
    statistics: dict


def _state_response(session_id: uuid.UUID, engine: ReplayEngine) -> ReplayStateResponse:
    trade = engine.open_trade
    return ReplayStateResponse(
        session_id=session_id,
        cursor=engine.clock.cursor,
        total_candles=len(engine.clock.candles),
        status=engine.clock.status.value,
        balance=engine.balance,
        current_price=engine.clock.current_candle.close,
        open_trade=(
            {
                "direction": trade.direction.value,
                "entry_price": trade.entry_price,
                "quantity": trade.quantity,
                "stop": trade.stop,
                "target": trade.target,
            }
            if trade
            else None
        ),
        statistics=engine.statistics.__dict__,
    )


@router.post("", response_model=ReplayStateResponse)
async def create_replay_session(
    payload: CreateReplayRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReplayStateResponse:
    candles = await get_candles(db, payload.instrument_id, payload.timeframe)
    if not candles:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No historical candles available for this instrument/timeframe")

    session_id = uuid.uuid4()
    engine = ReplayEngine(candles, starting_balance=payload.starting_balance, smc_config=SMCConfig(swing_length=payload.swing_length))
    _SESSIONS[session_id] = engine
    return _state_response(session_id, engine)


@router.get("/{session_id}", response_model=ReplayStateResponse)
async def get_replay_state(session_id: uuid.UUID, user: User = Depends(get_current_user)) -> ReplayStateResponse:
    return _state_response(session_id, _get_session(session_id))


@router.post("/{session_id}/step", response_model=ReplayStateResponse)
async def step_replay(session_id: uuid.UUID, steps: int = 1, user: User = Depends(get_current_user)) -> ReplayStateResponse:
    engine = _get_session(session_id)
    engine.advance(steps)
    return _state_response(session_id, engine)


@router.post("/{session_id}/reset", response_model=ReplayStateResponse)
async def reset_replay(session_id: uuid.UUID, user: User = Depends(get_current_user)) -> ReplayStateResponse:
    engine = _get_session(session_id)
    engine.clock.reset()
    engine.open_trade = None
    engine.closed_trades = []
    engine.balance = engine.starting_balance
    return _state_response(session_id, engine)


class ReplayOrderRequest(BaseModel):
    action: str  # "buy" | "sell" | "close" | "set_stop" | "set_target"
    quantity: float | None = None
    price: float | None = None


@router.post("/{session_id}/order", response_model=ReplayStateResponse)
async def submit_replay_order(
    session_id: uuid.UUID, payload: ReplayOrderRequest, user: User = Depends(get_current_user)
) -> ReplayStateResponse:
    engine = _get_session(session_id)
    try:
        if payload.action == "buy":
            engine.buy(payload.quantity or 1)
        elif payload.action == "sell":
            engine.sell(payload.quantity or 1)
        elif payload.action == "close":
            engine.close(payload.price)
        elif payload.action == "set_stop":
            engine.set_stop(payload.price)
        elif payload.action == "set_target":
            engine.set_target(payload.price)
        else:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown action: {payload.action}")
    except ReplayError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    return _state_response(session_id, engine)
