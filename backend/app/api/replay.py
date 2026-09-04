import dataclasses
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database.session import get_db
from app.database.models.users import User
from app.market.repository import get_candles
from app.replay.engine import ReplayEngine, ReplayError
from app.replay.persistence import create_replay_session_row, get_owned_replay_session, reset_replay_session, sync_replay_session
from app.smc.engine import SMCConfig

router = APIRouter(prefix="/replay", tags=["replay"])

# In-memory engine registry — this is the live process's working state
# (the same role app.api.orders._STACKS plays for trading); the persisted
# `replay_sessions`/`replay_orders` rows (app.replay.persistence) are what
# survive a restart and what ownership checks are enforced against, so a
# session's existence here is necessary but never sufficient on its own.
_SESSIONS: dict[uuid.UUID, ReplayEngine] = {}


async def _get_owned_session(session_id: uuid.UUID, user: User, db: AsyncSession) -> ReplayEngine:
    engine = _SESSIONS.get(session_id)
    if engine is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Replay session not found")
    # Every session in _SESSIONS was created through create_replay_session
    # below, which always persists a row first — a user who doesn't own
    # this session gets the same 404 as one that doesn't exist, rather
    # than a 403 that would confirm the session exists at all.
    if await get_owned_replay_session(db, session_id, user.id) is None:
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
        # ReplayStatistics is `@dataclass(slots=True)` — it has no
        # `__dict__`, so `.__dict__` raised AttributeError on every single
        # call to any /replay/* endpoint until this was caught by an
        # actual integration test (tests/api/test_replay_persistence.py).
        statistics=dataclasses.asdict(engine.statistics),
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
    await create_replay_session_row(db, session_id, user.id, payload.instrument_id, payload.timeframe, engine)
    _SESSIONS[session_id] = engine
    return _state_response(session_id, engine)


@router.get("/{session_id}", response_model=ReplayStateResponse)
async def get_replay_state(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReplayStateResponse:
    return _state_response(session_id, await _get_owned_session(session_id, user, db))


@router.post("/{session_id}/step", response_model=ReplayStateResponse)
async def step_replay(
    session_id: uuid.UUID, steps: int = 1, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReplayStateResponse:
    engine = await _get_owned_session(session_id, user, db)
    engine.advance(steps)
    await sync_replay_session(db, session_id, engine)
    return _state_response(session_id, engine)


@router.post("/{session_id}/reset", response_model=ReplayStateResponse)
async def reset_replay(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReplayStateResponse:
    engine = await _get_owned_session(session_id, user, db)
    engine.clock.reset()
    engine.open_trade = None
    engine.closed_trades = []
    engine.balance = engine.starting_balance
    await reset_replay_session(db, session_id, engine)
    return _state_response(session_id, engine)


class ReplayOrderRequest(BaseModel):
    action: str  # "buy" | "sell" | "close" | "set_stop" | "set_target"
    quantity: float | None = None
    price: float | None = None


@router.post("/{session_id}/order", response_model=ReplayStateResponse)
async def submit_replay_order(
    session_id: uuid.UUID, payload: ReplayOrderRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReplayStateResponse:
    engine = await _get_owned_session(session_id, user, db)
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

    await sync_replay_session(db, session_id, engine)
    return _state_response(session_id, engine)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_replay_session(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> None:
    """Ends a replay session, freeing its in-memory engine. `_SESSIONS`
    has no automatic eviction — every created session stays in process
    memory until this is called or the process restarts. The persisted
    `replay_sessions`/`replay_orders` rows are left alone -- this only
    closes the live working copy, not the history."""
    await _get_owned_session(session_id, user, db)
    del _SESSIONS[session_id]
