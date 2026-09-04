import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.replay import ReplayOrder, ReplaySession, ReplayStatus
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
        instrument = Instrument(symbol=f"RPY{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
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
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_replay_session_and_trade_persist_to_postgres(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register(client, "replayowner")
        headers = {"Authorization": f"Bearer {token}"}
        instrument = await _make_instrument()

        try:
            r = client.post("/replay", json={"instrument_id": str(instrument.id), "timeframe": "15m"}, headers=headers)
            assert r.status_code == 200, r.text
            session_id = uuid.UUID(r.json()["session_id"])

            async with async_session_factory() as db:
                row = await db.get(ReplaySession, session_id)
                assert row is not None
                assert row.user_id == user_id
                assert row.status == ReplayStatus.CREATED

            r = client.post(f"/replay/{session_id}/order", json={"action": "buy", "quantity": 10}, headers=headers)
            assert r.status_code == 200, r.text

            r = client.post(f"/replay/{session_id}/step", params={"steps": 3}, headers=headers)
            assert r.status_code == 200, r.text

            async with async_session_factory() as db:
                row = await db.get(ReplaySession, session_id)
                # `advance()` moves the clock's cursor but only `play()`
                # (not exposed as an endpoint) sets status to PLAYING, so
                # the only externally-observable proof of a step
                # persisting is current_time moving forward.
                assert row.current_time > row.start_time

                order_row = (await db.execute(select(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))).scalar_one()
                assert order_row.direction == "LONG"
                assert float(order_row.quantity) == 10.0
                assert order_row.closed_at is None  # still open

            r = client.post(f"/replay/{session_id}/order", json={"action": "close"}, headers=headers)
            assert r.status_code == 200, r.text

            async with async_session_factory() as db:
                order_row = (await db.execute(select(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))).scalar_one()
                assert order_row.closed_at is not None
                assert order_row.pnl is not None

                session_row = await db.get(ReplaySession, session_id)
                assert float(session_row.balance) != float(session_row.starting_balance)
        finally:
            await _cleanup([user_id], instrument.id)


async def test_replay_reset_clears_persisted_orders(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register(client, "replayreset")
        headers = {"Authorization": f"Bearer {token}"}
        instrument = await _make_instrument()

        try:
            r = client.post("/replay", json={"instrument_id": str(instrument.id), "timeframe": "15m"}, headers=headers)
            session_id = uuid.UUID(r.json()["session_id"])

            client.post(f"/replay/{session_id}/order", json={"action": "buy", "quantity": 5}, headers=headers)
            client.post(f"/replay/{session_id}/order", json={"action": "close"}, headers=headers)

            async with async_session_factory() as db:
                count_before = len((await db.execute(select(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))).scalars().all())
                assert count_before == 1

            r = client.post(f"/replay/{session_id}/reset", headers=headers)
            assert r.status_code == 200, r.text

            async with async_session_factory() as db:
                remaining = (await db.execute(select(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))).scalars().all()
                assert remaining == []
                row = await db.get(ReplaySession, session_id)
                assert row.status == ReplayStatus.CREATED
                assert float(row.balance) == float(row.starting_balance)
        finally:
            await _cleanup([user_id], instrument.id)


async def test_user_cannot_access_another_users_replay_session(require_infra):
    with TestClient(app) as client:
        owner_token, owner_id = await _register(client, "replayowner2")
        other_token, other_id = await _register(client, "replayother")
        instrument = await _make_instrument()

        try:
            r = client.post(
                "/replay", json={"instrument_id": str(instrument.id), "timeframe": "15m"}, headers={"Authorization": f"Bearer {owner_token}"}
            )
            session_id = r.json()["session_id"]

            other_headers = {"Authorization": f"Bearer {other_token}"}
            r = client.get(f"/replay/{session_id}", headers=other_headers)
            assert r.status_code == 404, r.text

            r = client.post(f"/replay/{session_id}/step", headers=other_headers)
            assert r.status_code == 404, r.text

            r = client.post(f"/replay/{session_id}/order", json={"action": "buy", "quantity": 1}, headers=other_headers)
            assert r.status_code == 404, r.text

            r = client.delete(f"/replay/{session_id}", headers=other_headers)
            assert r.status_code == 404, r.text

            # The owner can still use their own session.
            owner_headers = {"Authorization": f"Bearer {owner_token}"}
            r = client.get(f"/replay/{session_id}", headers=owner_headers)
            assert r.status_code == 200, r.text

            r = client.delete(f"/replay/{session_id}", headers=owner_headers)
            assert r.status_code == 204, r.text
            r = client.get(f"/replay/{session_id}", headers=owner_headers)
            assert r.status_code == 404, r.text
        finally:
            await _cleanup([owner_id, other_id], instrument.id)
