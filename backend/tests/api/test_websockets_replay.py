import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from starlette.testclient import WebSocketDisconnect

from app.database.models.instruments import Instrument, MarketType
from app.database.models.replay import ReplayOrder, ReplaySession
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio

_UNIT = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
    (104, 130, 104, 128),
]


async def _register(client: TestClient, label: str) -> tuple[str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    return token, user_id


async def _make_instrument() -> Instrument:
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(_UNIT * 2)]
    async with async_session_factory() as db:
        instrument = Instrument(symbol=f"WSR{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", candles)
        await db.commit()
        await db.refresh(instrument)
        return instrument


async def _cleanup(user_ids: list[uuid.UUID], instrument_id: uuid.UUID) -> None:
    from app.database.models.market import Candle as CandleRow

    async with async_session_factory() as db:
        session_ids = (await db.execute(select(ReplaySession.id).where(ReplaySession.instrument_id == instrument_id))).scalars().all()
        for session_id in session_ids:
            await db.execute(delete(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))
        await db.execute(delete(ReplaySession).where(ReplaySession.instrument_id == instrument_id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        for user_id in user_ids:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_ws_replay_broadcasts_state_after_step(require_infra):
    # Regression test: /ws/replay had no publisher anywhere -- every other
    # websocket channel (market/chart/scanner/signals/orders/positions)
    # has a matching `publish()` call, but stepping/ordering/resetting a
    # replay session never called it, so a connected client would simply
    # hang forever no matter what the session actually did.
    with TestClient(app) as client:
        token, user_id = await _register(client, "wsreplay")
        headers = {"Authorization": f"Bearer {token}"}
        instrument = await _make_instrument()

        try:
            r = client.post("/replay", json={"instrument_id": str(instrument.id), "timeframe": "15m"}, headers=headers)
            assert r.status_code == 200, r.text
            session_id = r.json()["session_id"]

            with client.websocket_connect(f"/ws/replay?session_id={session_id}&token={token}") as ws:
                time.sleep(0.2)  # let the redis SUBSCRIBE land before the mutation publishes
                r = client.post(f"/replay/{session_id}/step", headers=headers)
                assert r.status_code == 200, r.text

                message = ws.receive_json()
                assert message["session_id"] == session_id
                assert message["cursor"] >= 1
        finally:
            await _cleanup([user_id], instrument.id)


async def test_ws_replay_rejects_non_owner(require_infra):
    # Regression test: ws_replay authenticated the caller but never
    # checked they owned session_id -- any authenticated user could watch
    # another user's private replay session (balance, trades, P&L) just
    # by knowing its UUID, the same ownership bug already fixed twice
    # this project for the REST endpoints (/replay/*, /paper/*).
    with TestClient(app) as client:
        owner_token, owner_id = await _register(client, "wsreplayowner")
        other_token, other_id = await _register(client, "wsreplayother")
        instrument = await _make_instrument()

        try:
            r = client.post(
                "/replay", json={"instrument_id": str(instrument.id), "timeframe": "15m"}, headers={"Authorization": f"Bearer {owner_token}"}
            )
            session_id = r.json()["session_id"]

            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/ws/replay?session_id={session_id}&token={other_token}"):
                    pass

            # The owner's own connection still works.
            with client.websocket_connect(f"/ws/replay?session_id={session_id}&token={owner_token}"):
                pass
        finally:
            await _cleanup([owner_id, other_id], instrument.id)
