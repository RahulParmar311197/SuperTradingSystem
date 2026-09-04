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
from app.market.repository import upsert_candles
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
