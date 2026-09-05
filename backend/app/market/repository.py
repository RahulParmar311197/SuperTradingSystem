"""Candle persistence helpers shared by the market/charts/backtest APIs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.market import Candle as CandleRow
from app.smc.types import Candle


async def get_candles(
    db: AsyncSession, instrument_id: uuid.UUID, timeframe: str, start: datetime | None = None, end: datetime | None = None
) -> list[Candle]:
    stmt = select(CandleRow).where(CandleRow.instrument_id == instrument_id, CandleRow.timeframe == timeframe)
    if start is not None:
        stmt = stmt.where(CandleRow.timestamp >= start)
    if end is not None:
        stmt = stmt.where(CandleRow.timestamp <= end)
    stmt = stmt.order_by(CandleRow.timestamp)

    rows = (await db.execute(stmt)).scalars().all()
    return [
        Candle(
            timestamp=row.timestamp,
            open=float(row.open),
            high=float(row.high),
            low=float(row.low),
            close=float(row.close),
            volume=float(row.volume),
        )
        for row in rows
    ]


async def upsert_candles(db: AsyncSession, instrument_id: uuid.UUID, timeframe: str, candles: list[Candle]) -> None:
    """Despite the name, this used to be a plain `INSERT` -- any caller
    that ever re-persists a `(instrument_id, timeframe, timestamp)` combo
    already written (a worker restart replaying a backfill, an
    out-of-order tick, a derived-timeframe recompute racing a previous
    one) hit `uq_candle_key` and raised `UniqueViolationError`, which
    `app/workers/main.py`'s generic `except Exception` around this whole
    pipeline silently swallowed -- dropping the write, the tick's
    in-memory bookkeeping, and any signal of the failure all at once. A
    real `INSERT ... ON CONFLICT DO UPDATE` makes a re-write of the same
    bucket a safe, idempotent overwrite instead of a crash."""
    if not candles:
        return
    stmt = insert(CandleRow).values(
        [
            {
                "instrument_id": instrument_id,
                "timeframe": timeframe,
                "timestamp": candle.timestamp,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ]
    )
    stmt = stmt.on_conflict_do_update(
        constraint="uq_candle_key",
        set_={"open": stmt.excluded.open, "high": stmt.excluded.high, "low": stmt.excluded.low, "close": stmt.excluded.close, "volume": stmt.excluded.volume},
    )
    await db.execute(stmt)
    await db.commit()
