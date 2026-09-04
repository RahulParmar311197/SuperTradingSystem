import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database.models.instruments import Instrument, MarketType
from app.database.models.users import User
from app.database.session import get_db
from app.market.repository import get_candles

router = APIRouter(tags=["markets"])

# Simple in-process last-traded-price cache, populated by whatever feed is
# running (see app.market.feed). A single-process dev deployment only —
# swap for Redis (per blueprint §65) once running multiple workers.
_LATEST_QUOTES: dict[str, float] = {}


def set_latest_quote(symbol: str, ltp: float) -> None:
    _LATEST_QUOTES[symbol] = ltp


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
    instrument = Instrument(**payload.model_dump())
    db.add(instrument)
    await db.commit()
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
    return [CandleResponse(**c.__dict__) for c in candles]


@router.get("/quotes")
async def get_quotes(symbols: list[str] = Query(...), user: User = Depends(get_current_user)) -> dict[str, float | None]:
    return {symbol: _LATEST_QUOTES.get(symbol) for symbol in symbols}
