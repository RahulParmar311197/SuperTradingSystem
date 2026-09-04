import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database.models import Instrument
from app.database.session import get_db

router = APIRouter(prefix="/instruments", tags=["markets"])


@router.get("")
def list_instruments(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(Instrument).where(Instrument.active.is_(True))).scalars().all()
    return [
        {
            "id": str(row.id),
            "symbol": row.symbol,
            "exchange": row.exchange,
            "market": row.market,
            "instrument_type": row.instrument_type,
        }
        for row in rows
    ]


@router.get("/{instrument_id}")
def get_instrument(instrument_id: uuid.UUID, db: Session = Depends(get_db)) -> dict | None:
    row = db.get(Instrument, instrument_id)
    if row is None:
        return None
    return {
        "id": str(row.id),
        "symbol": row.symbol,
        "exchange": row.exchange,
        "market": row.market,
        "instrument_type": row.instrument_type,
        "lot_size": row.lot_size,
        "tick_size": row.tick_size,
    }
