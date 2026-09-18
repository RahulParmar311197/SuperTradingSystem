import dataclasses
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.redis import channel_name, publish
from app.database.session import get_db
from app.database.models.users import User
from app.api.charts import _serialize_ict, _serialize_smc
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
    starting_balance: float = Field(default=100_000.0, gt=0, lt=1e12)
    # Same bounds as `GET /charts/{id}/smc`, and for the same reason:
    # below 1 `detect_swings` raises, above ~100 the pivot window makes
    # the scan cost grow without telling anyone anything. This one is
    # currently inert -- `ReplayEngine.analyze` has no caller in `app/`,
    # so the `SMCConfig` built here is never used -- but the field is
    # accepted, stored on the engine, and one wiring-up away from being
    # the same 500 the charts overlay was.
    swing_length: int = Field(default=3, ge=1, le=100)


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


async def _publish_state(session_id: uuid.UUID, engine: ReplayEngine) -> ReplayStateResponse:
    """Broadcasts the post-mutation state on `/ws/replay?session_id=...`
    (blueprint §64). Every other websocket channel (market/chart/scanner/
    signals/orders/positions) has a matching publisher; this one didn't --
    `ws_replay` would accept a connection and then simply hang forever,
    since nothing ever called `publish` on its channel."""
    response = _state_response(session_id, engine)
    await publish(channel_name("replay", str(session_id)), response.model_dump())
    return response


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
    return await _publish_state(session_id, engine)


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
    return await _publish_state(session_id, engine)


@router.get("/{session_id}/analysis")
async def get_replay_analysis(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """SMC/ICT structure as of the replay cursor (blueprint §41).

    The blueprint's replay flow is Clock -> Current Timestamp -> only
    historical information available so far -> SMC/ICT -> Strategy -> AI
    -> Virtual Execution, and studying structure as it forms is the point
    of stepping bar by bar. `ReplayEngine.analyze` implemented that stage
    correctly and look-ahead-safely from the day it was written, and had
    no caller anywhere in `app/` -- this route is the missing one.

    That absence had a second cost. `ReplayClock.visible_candles`, whose
    docstring calls itself "the single rule that matters here" and which
    is what blueprint §45 requires, had exactly one consumer:
    `ReplayEngine.analyze`. With nothing calling that, the mandatory
    look-ahead guarantee was protecting nothing reachable and no test
    exercised it end to end. `tests/replay/test_replay_analysis.py` now
    asserts through this endpoint that no returned structure carries a
    timestamp later than the candle the cursor is sitting on.

    The payload reuses the chart overlay serialisers so the replay and
    chart views of the same structure cannot drift apart.
    """
    engine = await _get_owned_session(session_id, user, db)
    smc_context, ict_context = engine.analyze()
    return {
        "cursor": engine.clock.cursor,
        "as_of": engine.clock.current_candle.timestamp.isoformat(),
        "smc": _serialize_smc(smc_context),
        "ict": _serialize_ict(ict_context),
    }


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
    return await _publish_state(session_id, engine)


# Every number on this request is written to `replay_orders`, whose
# quantity/price/stop/target columns are `Numeric(18, 6)` -- the same
# ceiling `app/api/orders.py`'s `_MAX_PRICE` was measured against, for the
# same reason. Measured on the live endpoint before this bound:
# `quantity=1e308` and `quantity=Infinity` both 500 with
# NumericValueOutOfRangeError from `sync_replay_session`, `set_stop` with
# `price=1e308` does the same, and `quantity=NaN` is accepted at entry and
# then 500s on the close -- leaving the engine's trade closed in memory
# with nothing written for it.
_MAX_REPLAY_NUMERIC = 1e12

# `close` takes an optional price (no price means "at this candle's
# close", which is the documented behaviour). These two do not: a stop or
# a target *is* the price, so there is nothing for the engine to fall back
# to -- see `_require_a_price_where_the_action_is_the_price`.
_ACTIONS_THAT_ARE_A_PRICE = ("set_stop", "set_target")


class ReplayOrderRequest(BaseModel):
    action: str  # "buy" | "sell" | "close" | "set_stop" | "set_target"
    # `gt=0` rejects the non-finite values too: `NaN` fails every
    # comparison and `inf` fails `lt`, both verified against this pydantic
    # version rather than assumed. It also closes the second half of the
    # quantity bug -- `quantity=0` used to reach `payload.quantity or 1`
    # below and be *silently traded as 1 unit*, so a client that asked for
    # nothing got a position.
    quantity: float | None = Field(default=None, gt=0, lt=_MAX_REPLAY_NUMERIC)
    price: float | None = Field(default=None, gt=0, lt=_MAX_REPLAY_NUMERIC)

    @model_validator(mode="after")
    def _require_a_price_where_the_action_is_the_price(self) -> "ReplayOrderRequest":
        """`set_stop`/`set_target` with no price used to answer 200 and
        silently *clear* the stop.

        `engine.set_stop(payload.price)` assigns whatever it is given, so
        `{"action": "set_stop"}` set `stop = None`. Measured: a long with
        a stop at 99.5, stepped five bars through a low of 96, closes at
        99.5 for a 0.5 loss; the same long after one no-price `set_stop`
        is still open and unprotected at the end of those five bars, and
        the call that disarmed it reported success.

        Rejecting it rather than treating it as "clear the stop": there is
        no clear-the-stop action in blueprint §43's flow (SET SL, MOVE SL),
        and an accidental one must not be spelled the same way as setting
        one.
        """
        if self.action in _ACTIONS_THAT_ARE_A_PRICE and self.price is None:
            raise ValueError(f"{self.action} requires a price")
        return self


@router.post("/{session_id}/order", response_model=ReplayStateResponse)
async def submit_replay_order(
    session_id: uuid.UUID, payload: ReplayOrderRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ReplayStateResponse:
    engine = await _get_owned_session(session_id, user, db)
    try:
        # `is not None`, not `or 1`: with the bound above a zero quantity
        # is now a 422, but the `or` spelling is what turned it into a
        # silent 1-unit trade in the first place and should not survive as
        # the thing standing between that bug and this endpoint.
        quantity = payload.quantity if payload.quantity is not None else 1
        if payload.action == "buy":
            engine.buy(quantity)
        elif payload.action == "sell":
            engine.sell(quantity)
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
    return await _publish_state(session_id, engine)


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
