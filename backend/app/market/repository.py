"""Candle persistence helpers shared by the market/charts/backtest APIs."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
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
    for candle in candles:
        db.add(
            CandleRow(
                instrument_id=instrument_id,
                timeframe=timeframe,
                timestamp=candle.timestamp,
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
            )
        )
    await db.commit()
