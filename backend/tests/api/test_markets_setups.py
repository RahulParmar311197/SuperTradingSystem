import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.instruments import Instrument, MarketType
from app.database.models.risk import AuditLog
from app.database.models.strategy import Setup as SetupRow
from app.database.models.strategy import Signal as SignalRow
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle
from app.workers.scanner_worker import ScannerWorker

pytestmark = pytest.mark.asyncio

# Reuse the bullish sweep+FVG dataset already proven to produce structure
# events and a fair value gap in tests/workers/test_scanner_worker.py.
_SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
]


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    from app.database.models.market import Candle as CandleRow

    async with async_session_factory() as db:
        await db.execute(delete(SetupRow).where(SetupRow.instrument_id == instrument_id))
        await db.execute(delete(SignalRow).where(SignalRow.instrument_id == instrument_id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_get_setups_returns_scanner_journaled_detections(require_infra):
    # Regression test for the `setups` table (blueprint §9): it had zero
    # writers and zero readers anywhere before this round.
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(_SETUP)]

    async with async_session_factory() as db:
        instrument = Instrument(symbol=f"SETUP{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", candles)
        await db.commit()

    await ScannerWorker(timeframe="15m").run_once()

    with TestClient(app) as client:
        email = f"setups-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Setups Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        try:
            r = client.get("/setups", params={"instrument_id": str(instrument_id), "timeframe": "15m"}, headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body, "expected the scanner's journaled detections to be visible over the API"
            assert any(s["setup_type"] == "fair_value_gap" for s in body)
        finally:
            await _cleanup(user_id, instrument_id)
