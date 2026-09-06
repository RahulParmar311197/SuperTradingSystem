import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
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
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_positions_and_portfolio_reflect_live_unrealized_pnl(require_infra):
    # Regression test: `PositionManager.mark_to_market` correctly computes
    # unrealized_pnl, but nothing on the live/manual order path ever called
    # it -- `ExecutionEngine.submit`'s `apply_fill` only ever sets
    # quantity/average_price/realized_pnl. `GET /positions` and
    # `GET /portfolio` read `unrealized_pnl` straight off the in-memory
    # PositionManager, so both silently reported a permanent 0.0 no matter
    # how far the market moved after entry, unlike the paper-trading path
    # (PaperTradingEngine.on_candle already marks to market every candle).
    from app.core.redis import set_latest_price

    with TestClient(app) as client:
        email = f"posmtm-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Position MTM Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
        assert r.status_code == 200, r.text

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"POSMTM{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            r = client.get("/positions", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body) == 1
            assert body[0]["unrealized_pnl"] == pytest.approx(0.0)

            # Simulate a live tick arriving after entry -- this is what
            # MarketDataWorker.process_tick does in production.
            await set_latest_price(instrument.symbol, 120.0)

            r = client.get("/positions", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body) == 1
            quantity = body[0]["quantity"]
            assert body[0]["unrealized_pnl"] == pytest.approx((120.0 - 100.0) * quantity, rel=1e-6)
            assert body[0]["unrealized_pnl"] > 0

            r = client.get("/portfolio", headers=headers)
            assert r.status_code == 200, r.text
            portfolio = r.json()
            assert portfolio["total_unrealized_pnl"] == pytest.approx(body[0]["unrealized_pnl"], rel=1e-6)
        finally:
            await _cleanup(user_id, instrument_id)
