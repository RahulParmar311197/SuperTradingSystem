import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification, NotificationType
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.risk.engine import RiskEngine
from app.risk.limits import RiskCheck, RiskDecision, RiskDecisionResult

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
                # No broker account is connected for this user, so the
                # stack trades against MockBroker -- this must never be
                # journaled as LIVE (blueprint §101: "Never make paper and
                # live look identical").
                assert order_row.execution_mode == ExecutionMode.PAPER

                events = (await db.execute(select(OrderEvent).where(OrderEvent.order_id == order_row.id))).scalars().all()
                assert len(events) >= 4  # CREATED -> VALIDATING -> RISK_APPROVED -> SUBMITTED -> ...

                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert position_row.is_open is True
                assert float(position_row.quantity) > 0
                assert position_row.execution_mode == ExecutionMode.PAPER

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
                assert trade_row.execution_mode == ExecutionMode.PAPER
        finally:
            await _cleanup(user_id, instrument_id)


async def test_single_leg_option_order_persists_like_any_other_instrument(require_infra):
    """Blueprint §12 gives `instruments` strike/expiry/option_type
    directly — nothing in the order/execution/persistence pipeline
    branches on market type, so a single option contract should already
    place and close exactly like an equity does. (Multi-leg *strategy*
    execution — atomically submitting several legs together — is the
    real gap; see docs/PRODUCTION_READINESS.md.)"""
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"OPT{uuid.uuid4().hex[:6].upper()}",
                exchange="NSE",
                market=MarketType.OPTIONS,
                instrument_type="OPTION",
                underlying="NIFTY",
                expiry=date.today() + timedelta(days=7),
                strike=25000.0,
                option_type=OptionType.CALL,
                lot_size=50,
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 150.0, "stop": 100.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["status"] in ("FILLED", "MONITORING")

            async with async_session_factory() as db:
                order_row = (await db.execute(select(Order).where(Order.user_id == user_id))).scalar_one()
                assert order_row.instrument_id == instrument_id

                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert position_row.is_open is True

            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 180.0, "stop": 220.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                trade_row = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalar_one()
                assert float(trade_row.pnl) > 0  # bought the premium at 150, sold at 180
        finally:
            await _cleanup(user_id, instrument_id)


async def test_concurrent_stack_for_calls_share_one_stack(require_infra, monkeypatch):
    # Regression test: `_stack_for` checked `user.id not in _STACKS`, then
    # awaited `resolve_broker` (a real DB query), then wrote `_STACKS[user.id]`
    # -- with no lock across that await, two concurrent first calls for the
    # same user could each build their own `_UserTradingStack`, and the
    # second write would silently clobber the first, discarding whatever
    # in-memory order/position state the first stack already held.
    from app.api import orders as orders_module

    with TestClient(app) as client:
        email = f"orders-race-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Race Test"})
        assert r.status_code == 201, r.text

    async with async_session_factory() as db:
        user = (await db.execute(select(User).where(User.email == email))).scalar_one()

    real_resolve_broker = orders_module.resolve_broker

    async def _slow_resolve_broker(db, user):
        await asyncio.sleep(0.05)
        return await real_resolve_broker(db, user)

    monkeypatch.setattr(orders_module, "resolve_broker", _slow_resolve_broker)
    orders_module._STACKS.pop(user.id, None)
    orders_module._STACK_LOCKS.pop(user.id, None)

    try:
        async with async_session_factory() as db1, async_session_factory() as db2:
            stack1, stack2 = await asyncio.gather(
                orders_module._stack_for(user, db1), orders_module._stack_for(user, db2)
            )
        assert stack1 is stack2
        assert orders_module._STACKS[user.id] is stack1
    finally:
        orders_module._STACKS.pop(user.id, None)
        orders_module._STACK_LOCKS.pop(user.id, None)
        async with async_session_factory() as db:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user.id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user.id))
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()


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


async def test_place_order_rejected_by_risk_engine_notifies(require_infra, monkeypatch):
    # Regression test: unlike app/api/paper.py's feed_candle and
    # app/workers/auto_trade_worker.py's _process, this handler -- the one
    # order-placement path that handles real broker money -- used to only
    # write a RiskEvent audit row and raise a 403 on a risk rejection.
    # create_notification/NotificationType weren't even imported. No
    # Notification row was ever persisted, so nothing else (another
    # device, GET /notifications, an admin view) ever learned a live
    # order was blocked.
    monkeypatch.setattr(
        RiskEngine, "evaluate", lambda self, proposal: RiskDecisionResult(RiskDecision.REJECT, [], "forced rejection for test")
    )

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDREJ{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
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
            assert r.status_code == 403, r.text

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(select(Notification).where(Notification.user_id == user_id))
                ).scalars().all()
                assert len(notifications) == 1
                assert notifications[0].type == NotificationType.ORDER_REJECTED
                assert "forced rejection for test" in notifications[0].body
        finally:
            await _cleanup(user_id, instrument_id)


async def test_place_order_rejected_for_daily_loss_limit_notifies_distinctly(require_infra, monkeypatch):
    # Same gap as above, specifically for the daily-loss-limit case: the
    # paper/auto-trade paths already distinguish DAILY_LOSS_LIMIT from a
    # generic ORDER_REJECTED (see app/api/paper.py, app/workers/auto_trade_worker.py) --
    # the live-order path should behave the same way.
    monkeypatch.setattr(
        RiskEngine,
        "evaluate",
        lambda self, proposal: RiskDecisionResult(
            RiskDecision.REJECT,
            [RiskCheck("daily_loss_limit", False, "Daily loss 3.00% vs limit 2.0%")],
            "Daily loss 3.00% vs limit 2.0%",
        ),
    )

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDDL{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
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
            assert r.status_code == 403, r.text

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(select(Notification).where(Notification.user_id == user_id))
                ).scalars().all()
                assert len(notifications) == 1
                assert notifications[0].type == NotificationType.DAILY_LOSS_LIMIT
        finally:
            await _cleanup(user_id, instrument_id)
