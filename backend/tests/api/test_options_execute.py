import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register_and_grant_live_trade(client: TestClient) -> tuple[str, uuid.UUID]:
    email = f"optexec-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Options Exec Test"})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

    r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
    assert r.status_code == 200, r.text
    return token, user_id


async def _make_two_leg_instruments(underlying_prefix: str) -> tuple[Instrument, Instrument]:
    expiry = date.today() + timedelta(days=7)
    async with async_session_factory() as db:
        long_leg = Instrument(
            symbol=f"{underlying_prefix}25000CE",
            exchange="NSE",
            market=MarketType.OPTIONS,
            instrument_type="OPTION",
            underlying="NIFTY",
            expiry=expiry,
            strike=25000.0,
            option_type=OptionType.CALL,
            lot_size=50,
        )
        short_leg = Instrument(
            symbol=f"{underlying_prefix}25200CE",
            exchange="NSE",
            market=MarketType.OPTIONS,
            instrument_type="OPTION",
            underlying="NIFTY",
            expiry=expiry,
            strike=25200.0,
            option_type=OptionType.CALL,
            lot_size=50,
        )
        db.add_all([long_leg, short_leg])
        await db.commit()
        await db.refresh(long_leg)
        await db.refresh(short_leg)
        return long_leg, short_leg


async def _cleanup(user_id: uuid.UUID, instrument_ids: list[uuid.UUID]) -> None:
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
        for instrument_id in instrument_ids:
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_execute_bull_call_spread_persists_both_legs(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"BCS{uuid.uuid4().hex[:5].upper()}")

        try:
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 120.0},
                        {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 50.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()

            # net debit = (120 - 50) * 1 lot * 50 lot_size = 3500
            assert body["net_premium"] == pytest.approx(3500.0)
            assert body["max_loss"] == pytest.approx(-3500.0, rel=1e-3)
            assert len(body["legs"]) == 2
            for leg_result in body["legs"]:
                assert leg_result["status"] in ("FILLED", "MONITORING")
            # No option-chain ingestion pipeline exists in this environment
            # (see docs/ARCHITECTURE.md) -> every leg should warn, not reject.
            assert any("no liquidity data" in w for w in body["liquidity_warnings"])

            async with async_session_factory() as db:
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
                assert len(orders) == 2
                assert {o.instrument_id for o in orders} == {long_leg.id, short_leg.id}
                for order in orders:
                    assert float(order.quantity) == 50.0  # 1 lot * lot_size 50

                positions = (await db.execute(select(Position).where(Position.user_id == user_id))).scalars().all()
                assert len(positions) == 2
                assert all(p.is_open for p in positions)
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_execute_rejects_empty_legs(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            r = client.post("/options/execute", json={"strategy_name": "long_call", "legs": []}, headers=headers)
            assert r.status_code == 422, r.text
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
                await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()


async def test_execute_rejects_when_projected_loss_exceeds_exposure_limit(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"BIG{uuid.uuid4().hex[:5].upper()}")

        try:
            # 1000 lots * 50 lot_size * (120-50) net debit per unit -> a
            # multi-crore max_loss against a 100k mock balance.
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1000, "premium": 120.0},
                        {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 1000, "premium": 50.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 403, r.text

            async with async_session_factory() as db:
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
                assert orders == []  # nothing should have been placed
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id])
