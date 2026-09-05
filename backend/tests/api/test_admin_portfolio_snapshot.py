import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, PortfolioSnapshot, Position, Trade
from app.database.models.users import User, UserRole, UserSession
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
        await db.execute(delete(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id))
        await db.execute(delete(RiskEvent).where(RiskEvent.user_id == user_id))
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_admin_portfolio_snapshot_journals_a_paper_accounts_real_exposure(require_infra):
    # Regression test for the `portfolio_snapshots` table (blueprint §9):
    # it had zero writers anywhere before this round.
    with TestClient(app) as client:
        email = f"pfsnap-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Snapshot Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
        assert r.status_code == 200, r.text

        async with async_session_factory() as db:
            admin_user = await db.get(User, user_id)
            admin_user.role = UserRole.ADMIN
            await db.commit()

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"PFS{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # Places the order and, as a side effect, creates this user's
            # entry in app.api.orders._STACKS -- snapshot_all_stacks only
            # sees stacks that already exist.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            r = client.post("/admin/portfolio-snapshot", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["accounts_snapshotted"] >= 1

            async with async_session_factory() as db:
                snapshot = (
                    await db.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id))
                ).scalar_one()
                assert snapshot.execution_mode == ExecutionMode.PAPER
                assert float(snapshot.total_exposure) == pytest.approx(10000.0, rel=1e-6)  # 100 shares * 100
                assert float(snapshot.balance) > 0
                # No option-chain data exists for a plain equity position.
                assert float(snapshot.net_delta) == 0.0
                assert float(snapshot.net_gamma) == 0.0
                assert float(snapshot.net_theta) == 0.0
                assert float(snapshot.net_vega) == 0.0
        finally:
            await _cleanup(user_id, instrument_id)


async def test_non_admin_cannot_trigger_portfolio_snapshot(require_infra):
    with TestClient(app) as client:
        email = f"pfsnap-noperm-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "No Perm"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        try:
            r = client.post("/admin/portfolio-snapshot", headers=headers)
            assert r.status_code == 403, r.text
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
                await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
