"""Worker-test-specific fixtures. See tests/conftest.py for `require_infra`."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import delete

from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.strategy import Setup as SetupRow
from app.database.models.strategy import Signal as SignalRow
from app.database.session import async_session_factory


@pytest.fixture
async def db_instrument(require_infra):
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"TEST{uuid.uuid4().hex[:8].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        yield instrument
        # clear anything the test created that references this instrument,
        # since the schema intentionally has no cascade delete here
        await db.execute(delete(SignalRow).where(SignalRow.instrument_id == instrument.id))
        await db.execute(delete(SetupRow).where(SetupRow.instrument_id == instrument.id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument.id))
        await db.delete(instrument)
        await db.commit()
