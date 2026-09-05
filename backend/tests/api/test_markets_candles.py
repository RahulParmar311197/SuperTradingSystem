import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import get_candles, upsert_candles
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_get_candles_returns_data_without_crashing(require_infra):
    # Regression test: GET /candles previously 500'd whenever it had
    # any candles to return (`Candle` is `@dataclass(frozen=True,
    # slots=True)`, and `c.__dict__` raises AttributeError on it) because
    # no test had ever actually exercised this endpoint before.
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), 100 + i, 101 + i, 99 + i, 100 + i, 10) for i in range(5)]

    async with async_session_factory() as db:
        instrument = Instrument(symbol=f"CDL{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", candles)

    with TestClient(app) as client:
        email = f"candles-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Candles Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        try:
            r = client.get(
                "/candles",
                params={"instrument_id": str(instrument_id), "timeframe": "15m"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body) == 5
            assert body[0]["close"] == 100
            assert set(body[0].keys()) == {"timestamp", "open", "high", "low", "close", "volume"}
        finally:
            await _cleanup(user_id, instrument_id)


async def test_upsert_candles_is_idempotent_on_conflict(require_infra):
    # Regression test: despite the name, `upsert_candles` used to be a
    # plain `INSERT` with no conflict handling. Any caller that
    # re-persists the same `(instrument_id, timeframe, timestamp)` combo
    # -- a worker restart replaying a backfill, an out-of-order tick in
    # app/workers/candle_worker.py mishandled as a rollover, a
    # derived-timeframe recompute racing a previous one -- hit
    # `uq_candle_key` and raised `UniqueViolationError`, which the
    # generic `except Exception` around the whole market-data pipeline
    # (app/workers/main.py) silently swallowed: the write, and the
    # worker's in-memory bookkeeping for that tick, both vanished with no
    # record anywhere. A real `INSERT ... ON CONFLICT DO UPDATE` makes a
    # re-write of the same bucket a safe overwrite instead of a crash.
    async with async_session_factory() as db:
        instrument = Instrument(symbol=f"UPS{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.commit()
        instrument_id = instrument.id

    ts = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    try:
        async with async_session_factory() as db:
            await upsert_candles(db, instrument_id, "1m", [Candle(ts, 100, 101, 99, 100, 10)])

        # Re-persisting the exact same bucket with different OHLC (as a
        # legitimate correction/replay would) must not raise, and must
        # overwrite rather than duplicate.
        async with async_session_factory() as db:
            await upsert_candles(db, instrument_id, "1m", [Candle(ts, 100, 150, 99, 140, 25)])

        async with async_session_factory() as db:
            persisted = await get_candles(db, instrument_id, "1m")
        assert len(persisted) == 1
        assert persisted[0].high == 150
        assert persisted[0].close == 140
        assert persisted[0].volume == 25
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
            await db.commit()
