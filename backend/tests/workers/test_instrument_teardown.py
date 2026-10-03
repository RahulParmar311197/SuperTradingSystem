"""Round 178b: a teardown that could not delete its own instrument.

The damage is not the `DELETE FROM instruments` raising -- it is the
rollback. A teardown deletes its user's rows and the instrument in one
transaction, so when the instrument delete fails the whole thing is
undone and the instrument survives **with its candles**. A 15m bar stays
inside `MAX_CANDLE_AGE_IN_BARS` for an hour, and for that hour
`AutoTradeSupervisor` keeps pairing the leaked instrument with every
later test's brand-new user and firing entries on it. That cost 25
failures across `tests/workers/test_auto_trade_*`, none of them in the
code under test.

What makes the delete fail is that two production actors write against
**every active instrument** rather than one they were handed, with rows
no user-scoped delete can reach:

  - `ScannerWorker` (app/workers/scanner_worker.py:48) writes `Signal`
    and `Setup` rows keyed by `instrument_id` alone -- they belong to no
    user at all.
  - `AutoTradeSupervisor.run_once` (app/workers/auto_trade_worker.py:214)
    pairs every eligible user's strategies with it, so another test's
    user holds `positions` and `orders` here.

Both are reproduced below as the rows they really are.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, text

from app.database.models.instruments import Instrument, MarketType
from app.database.models.strategy import Setup as SetupRow
from app.database.models.strategy import Signal as SignalRow
from app.database.models.strategy import Direction
from app.database.session import async_session_factory
from tests.instrument_cleanup import purge_instrument

# Set by the first test, asserted gone by the second. The `db_instrument`
# teardown runs between them, which is the only place its behaviour is
# observable -- a fixture cannot be asserted on from inside the test it
# is serving.
_CONTAMINATED_INSTRUMENT_ID: uuid.UUID | None = None


async def _add_scanner_rows(db, instrument_id: uuid.UUID) -> None:
    """Exactly what `ScannerWorker` writes, and keyed the same way."""
    db.add(
        SignalRow(
            instrument_id=instrument_id,
            timeframe="15m",
            direction=Direction.LONG,
            entry=100,
            stop=99,
            target=103,
            risk_reward=3.0,
            score=50.0,
            context={},
            generated_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        SetupRow(
            instrument_id=instrument_id,
            timeframe="15m",
            setup_type="fvg",
            data={},
            detected_at=datetime.now(timezone.utc),
        )
    )


async def test_purge_instrument_removes_rows_no_user_scoped_delete_can_reach(require_infra):
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"PRG{uuid.uuid4().hex[:8].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        instrument_id = instrument.id

    try:
        async with async_session_factory() as db:
            await _add_scanner_rows(db, instrument_id)
            await db.commit()

        async with async_session_factory() as db:
            await purge_instrument(db, instrument_id)
            await db.commit()

        async with async_session_factory() as db:
            assert (
                await db.execute(select(Instrument).where(Instrument.id == instrument_id))
            ).scalar_one_or_none() is None
            for table in ("signals", "setups"):
                remaining = (
                    await db.execute(
                        text(f"SELECT count(*) FROM {table} WHERE instrument_id = :i"),
                        {"i": instrument_id},
                    )
                ).scalar_one()
                assert remaining == 0, f"{table} still references the deleted instrument"
    finally:
        # Only reached if the purge above failed; otherwise a no-op.
        async with async_session_factory() as db:
            await purge_instrument(db, instrument_id)
            await db.commit()


async def test_the_db_instrument_fixture_contaminated_by_another_actor(db_instrument):
    """Leave rows on the fixture's instrument that it does not own.

    This test asserts nothing itself -- the assertion is the next one.
    Its job is to put the fixture's teardown in the position that used to
    break it: an instrument carrying `signals` and `setups` written by
    something other than the test that created it.
    """
    global _CONTAMINATED_INSTRUMENT_ID
    async with async_session_factory() as db:
        await _add_scanner_rows(db, db_instrument.id)
        await db.commit()
    _CONTAMINATED_INSTRUMENT_ID = db_instrument.id


async def test_the_contaminated_fixture_instrument_was_still_torn_down(require_infra):
    """The call-site assertion for the test above.

    A fixture's teardown runs after the test it served returns, so this
    is the only place it can be observed. Before round 178b the teardown
    raised here and rolled back, and this instrument -- with its candles
    -- outlived the run.
    """
    assert _CONTAMINATED_INSTRUMENT_ID is not None, "the previous test did not run"
    async with async_session_factory() as db:
        leaked = (
            await db.execute(
                select(Instrument).where(Instrument.id == _CONTAMINATED_INSTRUMENT_ID)
            )
        ).scalar_one_or_none()
    if leaked is not None:
        async with async_session_factory() as db:
            await purge_instrument(db, _CONTAMINATED_INSTRUMENT_ID)
            await db.commit()
        pytest.fail(
            f"the db_instrument teardown left {leaked.symbol} behind; a leaked instrument "
            "keeps its candles and stays tradeable by every later test for an hour"
        )
