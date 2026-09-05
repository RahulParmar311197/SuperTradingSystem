import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.encryption import encrypt_credentials
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification, NotificationType
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position, Trade
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User, UserSession
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
        await db.execute(delete(BrokerAccount).where(BrokerAccount.user_id == user_id))
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


async def test_place_order_records_which_broker_account_executed_it(require_infra):
    # Regression test: `Order.broker_account_id` is a real FK column that
    # existed purely to trace a placed order back to whichever connected
    # BrokerAccount executed it, but `resolve_broker` used to discard
    # `account.id` once it built the adapter, so this column was NULL for
    # every order ever placed -- silently. Uses an ACTIVE PAPER account
    # (resolves to MockBroker, same as the no-account default) specifically
    # so this test doesn't need real Upstox/Dhan credentials to prove the
    # id survives end-to-end through place_order -> persist_order.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDBA{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            account = BrokerAccount(
                user_id=user_id,
                broker=BrokerName.PAPER,
                encrypted_credentials=encrypt_credentials({}),
                status=BrokerAccountStatus.ACTIVE,
            )
            db.add(account)
            await db.commit()
            await db.refresh(instrument)
            await db.refresh(account)
            instrument_id = instrument.id
            account_id = account.id

        try:
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                order_row = (await db.execute(select(Order).where(Order.user_id == user_id))).scalar_one()
                assert order_row.broker_account_id == account_id
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


async def test_daily_loss_limit_is_enforced_after_a_real_realized_loss(require_infra):
    # Regression test: `_UserTradingStack.daily_pnl`/`weekly_pnl`
    # (app/api/orders.py) were initialized to 0.0 and never updated
    # anywhere else in the file -- unlike `PaperTradingEngine.daily_pnl`/
    # `weekly_pnl` (app/paper/engine.py), which correctly accumulate
    # realized P&L in `_maybe_exit`. That meant RiskEngine.evaluate's
    # daily_loss_limit check (app/risk/engine.py) could never fail for this
    # path -- proposal.daily_pnl was always 0 -- no matter how much the
    # account actually lost. Unlike the two tests above, this places a real
    # loss (no monkeypatching RiskEngine.evaluate) and confirms a
    # subsequent order is genuinely rejected because of it.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDDLR{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # Open a long position: risk_per_trade_pct=0.5% of a 100,000
            # MockBroker balance / (100-95) stop distance -> 100 shares.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            # Close it at a steep loss: 100 shares * (100 - 50) = 5,000
            # realized loss -- 5% of the 100,000 account balance, well past
            # the default 2% daily limit. This order is itself evaluated
            # *before* its own loss is realized, so it must still succeed.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 50.0, "stop": 55.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            # A third order must now be blocked by the daily loss limit --
            # before the fix, stack.daily_pnl was still 0.0 here and this
            # would have been approved.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "Daily loss" in r.text

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(select(Notification).where(Notification.user_id == user_id))
                ).scalars().all()
                assert any(n.type == NotificationType.DAILY_LOSS_LIMIT for n in notifications)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_user_trading_stack_resets_daily_and_weekly_counters_at_boundaries():
    # Regression test: `_UserTradingStack.trades_today`/`daily_pnl`/
    # `weekly_pnl` used to persist for the lifetime of the API process --
    # only a restart ever cleared them -- making RiskEngine.evaluate's
    # max_trades_per_day/daily_loss_limit/weekly_loss_limit checks
    # lifetime-of-process limits rather than the rolling daily/weekly
    # limits they're meant to be.
    from datetime import datetime, timedelta, timezone

    from app.api.orders import _UserTradingStack
    from app.brokers.mock import MockBroker

    stack = _UserTradingStack(MockBroker())
    monday = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
    stack._roll_risk_window(monday)
    stack.trades_today = 5
    stack.daily_pnl = -1500.0
    stack.weekly_pnl = -1500.0

    # Same day -- nothing resets.
    stack._roll_risk_window(monday + timedelta(hours=2))
    assert stack.trades_today == 5
    assert stack.daily_pnl == -1500.0
    assert stack.weekly_pnl == -1500.0

    # Next day, same ISO week -- daily counters reset, weekly does not.
    stack._roll_risk_window(monday + timedelta(days=1))
    assert stack.trades_today == 0
    assert stack.daily_pnl == 0.0
    assert stack.weekly_pnl == -1500.0

    # A week later -- a new ISO week -- weekly resets too.
    stack.trades_today = 3
    stack.daily_pnl = -200.0
    stack._roll_risk_window(monday + timedelta(days=7))
    assert stack.trades_today == 0
    assert stack.daily_pnl == 0.0
    assert stack.weekly_pnl == 0.0


async def test_repeated_broker_rejections_trip_the_circuit_breaker(require_infra):
    # Regression test: RiskEngine.evaluate's `no_repeated_rejections` check
    # (app/risk/engine.py, blueprint §57 "Repeated order rejection") reads
    # `proposal.repeated_rejections`, but nothing ever set it -- it was
    # always the TradeRiskProposal default of 0, so this check could never
    # fail no matter how many times an account's orders were rejected by
    # the broker in a row.
    from app.api import orders as orders_module

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDRJ{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # First order builds this user's stack -- fetch it and force
            # every subsequent broker submission to be rejected.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            stack = orders_module._STACKS[user_id]
            stack.broker.reject_probability = 1.0

            # 3 consecutive broker-rejected orders (distinct entry/stop so
            # each gets its own idempotency key and actually attempts
            # execution) -- default max_repeated_rejections is 3.
            for entry in (101.0, 102.0, 103.0):
                r = client.post(
                    "/orders",
                    json={"symbol": instrument.symbol, "direction": "LONG", "entry": entry, "stop": entry - 5},
                    headers=headers,
                )
                assert r.status_code == 201, r.text
                assert r.json()["status"] == "REJECTED"

            # A 4th attempt must now be blocked by the risk engine before it
            # ever reaches the broker -- before the fix, repeated_rejections
            # was always 0 and this would have gone through to (and been
            # rejected by) the broker again instead of being pre-emptively
            # blocked.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 104.0, "stop": 99.0},
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "no_repeated_rejections" in r.text

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(select(Notification).where(Notification.user_id == user_id))
                ).scalars().all()
                assert any(n.type == NotificationType.ORDER_REJECTED for n in notifications)
        finally:
            await _cleanup(user_id, instrument_id)
