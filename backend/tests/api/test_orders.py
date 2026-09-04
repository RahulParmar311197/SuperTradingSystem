import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        for order_id in order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Trade).where(Trade.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        await db.execute(delete(RiskEvent).where(RiskEvent.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def _register_and_grant_live_trade(client: TestClient) -> tuple[str, uuid.UUID]:
    email = f"orders-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Orders Test"})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

    r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
    assert r.status_code == 200, r.text
    assert "LIVE_TRADE" in r.json()["permissions"]
    return token, user_id


async def test_place_order_requires_live_trade_permission(require_infra):
    with TestClient(app) as client:
        email = f"orders-noperm-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "No Perm"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        r = client.post(
            "/orders",
            json={"symbol": "DOESNOTEXIST", "direction": "LONG", "entry": 100.0, "stop": 95.0},
            headers=headers,
        )
        assert r.status_code == 403, r.text

        async with async_session_factory() as db:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_place_and_close_order_persists_to_database(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORD{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # Open a long position.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["status"] in ("FILLED", "MONITORING")

            async with async_session_factory() as db:
                order_row = (await db.execute(select(Order).where(Order.user_id == user_id))).scalar_one()
                assert order_row.instrument_id == instrument_id
                assert order_row.status.value in ("FILLED", "MONITORING")

                events = (await db.execute(select(OrderEvent).where(OrderEvent.order_id == order_row.id))).scalars().all()
                assert len(events) >= 4  # CREATED -> VALIDATING -> RISK_APPROVED -> SUBMITTED -> ...

                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert position_row.is_open is True
                assert float(position_row.quantity) > 0

            # Close it with an opposing SHORT fill at a higher price -> realized profit.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 110.0, "stop": 115.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert position_row.is_open is False

                trade_row = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalar_one()
                # 100 shares bought at 100, sold at 110 -> 1000 realized profit.
                assert float(trade_row.pnl) == pytest.approx(1000.0, rel=1e-6)
                assert trade_row.direction.value == "LONG"
        finally:
            await _cleanup(user_id, instrument_id)


async def test_place_order_unknown_symbol_returns_404(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        r = client.post(
            "/orders",
            json={"symbol": "NOSUCHINSTRUMENT", "direction": "LONG", "entry": 100.0, "stop": 95.0},
            headers=headers,
        )
        assert r.status_code == 404, r.text

        async with async_session_factory() as db:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
