"""Worker-test-specific fixtures. See tests/conftest.py for `require_infra`."""

from __future__ import annotations

import uuid

import pytest

from app.database.models.instruments import Instrument, MarketType
from app.database.session import async_session_factory
from tests.instrument_cleanup import purge_instrument


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
        # Clear everything referencing this instrument, since the schema
        # intentionally has no cascade delete here. Scoped by instrument,
        # not by user: `AutoTradeSupervisor.run_once` pairs *every*
        # eligible user with every active instrument, so another test's
        # user routinely holds positions/orders here. Leaving those
        # behind made `delete(instrument)` raise
        # `positions_instrument_id_fkey` and roll the whole teardown
        # back, leaking the instrument *with its fresh candles* -- see
        # tests/instrument_cleanup.py for what that then cost.
        await purge_instrument(db, instrument.id)
        await db.commit()
