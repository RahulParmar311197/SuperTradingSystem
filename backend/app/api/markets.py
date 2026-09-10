import dataclasses
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.redis import get_latest_price
from app.database.models.instruments import Instrument, MarketType
from app.database.models.strategy import Setup
from app.database.models.users import User
from app.database.session import get_db
from app.market.repository import get_candles

router = APIRouter(tags=["markets"])


@router.get("/markets")
async def list_markets(user: User = Depends(get_current_user)) -> list[str]:
    return [m.value for m in MarketType]


class InstrumentResponse(BaseModel):
    id: uuid.UUID
    symbol: str
    exchange: str
    market: str
    instrument_type: str
    lot_size: int
    tick_size: float
    active: bool

    model_config = {"from_attributes": True}


class InstrumentCreateRequest(BaseModel):
    symbol: str
    exchange: str
    market: MarketType
    instrument_type: str
    lot_size: int = 1
    tick_size: float = 0.05
    currency: str = "INR"


@router.get("/instruments", response_model=list[InstrumentResponse])
async def list_instruments(
    market: MarketType | None = None,
    symbol: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Instrument]:
    stmt = select(Instrument).where(Instrument.active.is_(True))
    if market is not None:
        stmt = stmt.where(Instrument.market == market)
    if symbol is not None:
        stmt = stmt.where(Instrument.symbol == symbol)
    return (await db.execute(stmt)).scalars().all()


@router.post("/instruments", response_model=InstrumentResponse, status_code=status.HTTP_201_CREATED)
async def create_instrument(
    payload: InstrumentCreateRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Instrument:
    """Registers a symbol in the global instrument master.

    Refuses a symbol that already exists. `instruments` is un-owned and
    shared by every user, and every reader of it resolves a symbol with
    `.scalar_one_or_none()` -- app/api/orders.py's
    `_get_instrument_by_symbol`, app/api/options.py's per-leg lookup,
    app/risk/portfolio.py's `compute_correlated_exposure`, and
    app/trading/portfolio_snapshots.py. A second row for one symbol made
    every one of them raise `MultipleResultsFound`, which the app's
    catch-all handler turns into a 500.

    That is worse than it sounds, because `_get_instrument_by_symbol` is
    the *first* statement of `place_order`: the failure lands before
    `is_reducing` is even computed, so the exit-order exemption that
    exists precisely so a position can always be closed never runs. A
    single duplicate registration -- an operator re-running a bootstrap
    script, not necessarily anyone malicious -- locked every holder of
    that symbol into their open position with no way out through the API,
    since POST /orders is the only path that closes one and there is no
    endpoint to delete or deactivate an instrument.
    """
    existing = (
        await db.execute(select(Instrument).where(Instrument.symbol == payload.symbol))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Instrument symbol {payload.symbol!r} is already registered (id {existing.id}).",
        )

    instrument = Instrument(**payload.model_dump())
    db.add(instrument)
    try:
        await db.commit()
    except IntegrityError as exc:
        # The check above is read-then-write, so two concurrent
        # registrations of the same symbol can both pass it. The unique
        # index on `instruments.symbol` is what actually guarantees the
        # invariant; this turns the loser of that race into the same 409
        # rather than a 500.
        await db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Instrument symbol {payload.symbol!r} is already registered."
        ) from exc
    await db.refresh(instrument)
    return instrument


class CandleResponse(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


@router.get("/candles", response_model=list[CandleResponse])
async def get_candles_endpoint(
    instrument_id: uuid.UUID,
    timeframe: str,
    start: datetime | None = None,
    end: datetime | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CandleResponse]:
    candles = await get_candles(db, instrument_id, timeframe, start, end)
    # Candle is `@dataclass(frozen=True, slots=True)` — no `__dict__`
    # attribute; GET /markets/candles had never had a test hit it, so it
    # 500'd on every call with any candles to return.
    return [CandleResponse(**dataclasses.asdict(c)) for c in candles]


class SetupResponse(BaseModel):
    setup_type: str
    data: dict
    detected_at: datetime

    model_config = {"from_attributes": True}


@router.get("/setups", response_model=list[SetupResponse])
async def list_setups(
    instrument_id: uuid.UUID,
    timeframe: str,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Setup]:
    """Raw SMC pattern detections journaled by `ScannerWorker` (blueprint
    §9 `setups`) -- structure breaks, fair value gaps, order blocks --
    independent of whether any strategy matched on them. Backs blueprint
    §96 "AI Screen" prompts like "Explain this FVG."""
    stmt = (
        select(Setup)
        .where(Setup.instrument_id == instrument_id, Setup.timeframe == timeframe)
        .order_by(Setup.detected_at.desc())
        .limit(limit)
    )
    return (await db.execute(stmt)).scalars().all()


@router.get("/quotes")
async def get_quotes(symbols: list[str] = Query(...), user: User = Depends(get_current_user)) -> dict[str, float | None]:
    return {symbol: await get_latest_price(symbol) for symbol in symbols}
