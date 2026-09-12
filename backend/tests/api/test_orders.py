import asyncio
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.brokers.base import AccountInfo, Broker, BrokerError, BrokerOrder, BrokerPosition, OrderRequest, OrderResult, Quote
from app.brokers.mock import MockBroker as _MockBrokerBase
from app.core.encryption import encrypt_credentials
from app.core.redis import account_halt_reason, resume_account
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification, NotificationType
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, OrderStatus, Position, Trade
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User, UserSession
from app.database.session import async_session_factory
from app.main import app
import app.api.orders as orders_api
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


async def test_partial_close_journals_only_the_quantity_actually_closed(require_infra):
    # Regression test: the `record_trade` call fires on any non-zero
    # `realized_delta`, which includes a *partial* reduce, but it passed
    # `quantity=abs(position_before["quantity"])` -- the whole pre-fill
    # position. Closing 40 of a 100-unit position therefore journaled
    # `quantity=100` next to a `pnl` covering only those 40 units, so the
    # row did not agree with itself: 100 units moving 100 -> 120 is 2000,
    # not the 800 recorded. Summing `trades.quantity` across the position
    # also double-counted, reaching 160 units for a 100-unit position.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"PART{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
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

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                opened_quantity = float(position_row.quantity)
            assert opened_quantity > 1

            # Close only part of it, at a profit. POST /orders has no
            # `quantity` field -- it always sizes the order itself as
            # `balance * risk_per_trade_pct / abs(entry - stop)` -- so a
            # partial reduce is produced by giving the closing order a
            # *wider* stop than the opening one, which buys fewer units.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 110.0, "stop": 130.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            partial = float(r.json()["quantity"])
            assert 0 < partial < opened_quantity, "expected the closing order to be smaller than the open position"

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert position_row.is_open is True, "expected a partial close to leave the position open"
                assert float(position_row.quantity) == pytest.approx(opened_quantity - partial, rel=1e-6)

                trade_row = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalar_one()
                journaled_quantity = float(trade_row.quantity)
                pnl = float(trade_row.pnl)
                entry_price = float(trade_row.entry_price)
                exit_price = float(trade_row.exit_price)

            # The row must describe the fill that actually happened, not the
            # position that existed before it.
            assert journaled_quantity == pytest.approx(partial, rel=1e-6)
            # And it must be internally consistent: quantity * (exit - entry)
            # has to reproduce the recorded pnl.
            assert journaled_quantity * (exit_price - entry_price) == pytest.approx(pnl, rel=1e-6)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_live_order_notifies_on_trade_executed_and_position_closed(require_infra):
    # Regression test: `place_order` only ever called `create_notification`
    # on risk rejection -- a real fill that opens or closes a live
    # position never notified at all, unlike app/api/paper.py and
    # app/workers/auto_trade_worker.py, which both already fire
    # TRADE_EXECUTED/POSITION_CLOSED for the identical event. Blueprint
    # §63/§104 list these as required notification events with no
    # live/paper carve-out -- if anything the live path (real broker
    # money) is the one where this matters most.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDNOTIF{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
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

            async with async_session_factory() as db:
                notifications = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            assert len(notifications) == 1
            assert notifications[0].type == NotificationType.TRADE_EXECUTED

            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 110.0, "stop": 115.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                notifications = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            assert len(notifications) == 2
            assert {n.type for n in notifications} == {NotificationType.TRADE_EXECUTED, NotificationType.POSITION_CLOSED}
            closed = next(n for n in notifications if n.type == NotificationType.POSITION_CLOSED)
            assert "1000" in closed.body
        finally:
            await _cleanup(user_id, instrument_id)


async def test_live_order_records_stop_on_the_resulting_position(require_infra):
    # Regression test: `payload.stop` is required input already used to
    # size the order (calculate_position_size) and feed the risk engine,
    # but nothing ever attached it to the resulting position -- unlike
    # PaperTradingEngine, which already records a stop for every
    # paper/auto-trade entry. Every live position's stop was permanently
    # None, so the positions.stop column was dead-on-arrival and nothing
    # could ever know what a position's protective level was supposed to
    # be.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDSTP{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # Opening a position records its stop.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert float(position_row.stop) == pytest.approx(95.0)

            # Adding to the same-direction position updates the stop to
            # whatever this new order declares.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 101.0, "stop": 94.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert float(position_row.stop) == pytest.approx(94.0)

            # A reducing fill in the opposite direction must never
            # overwrite the stop with its own (irrelevant) value -- a
            # large stop distance keeps this order's sized quantity small
            # relative to the existing open position, so it only reduces,
            # not closes or flips it.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 110.0, "stop": 10.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position_row = (await db.execute(select(Position).where(Position.user_id == user_id))).scalar_one()
                assert position_row.is_open is True
                assert float(position_row.stop) == pytest.approx(94.0)
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


async def test_abnormal_price_jump_blocks_a_new_order(require_infra):
    # Regression test: RiskEngine.evaluate's `no_abnormal_price_jump` check
    # (app/risk/engine.py, blueprint §57 "unexpected price jump") reads
    # `proposal.recent_price_jump_pct`, but nothing ever computed it -- it
    # was always the TradeRiskProposal default of 0.0, so this check could
    # never fail no matter how violently a symbol's price moved between
    # ticks.
    from app.core.redis import set_latest_price

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDJMP{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # Two ticks 10% apart -- well past the default 5% limit. Also
            # keeps market_data_age_seconds fresh (near 0), so that check
            # doesn't confound this one.
            await set_latest_price(instrument.symbol, 100.0)
            await set_latest_price(instrument.symbol, 110.0)

            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 110.0, "stop": 105.0},
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "no_abnormal_price_jump" in r.text

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(select(Notification).where(Notification.user_id == user_id))
                ).scalars().all()
                assert any(n.type == NotificationType.ORDER_REJECTED for n in notifications)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_account_kill_switch_blocks_a_new_order(require_infra):
    # Regression test: `KillSwitchState` (app/risk/kill_switch.py, blueprint
    # §58) was a plain in-memory dataclass that nothing anywhere ever
    # mutated -- there was no admin endpoint, and even if one existed, a
    # kill set on one process's object couldn't reach the RiskEngine living
    # inside another process's (or this same process's other) per-user
    # trading stack. `RiskEngine.evaluate`'s "kill_switch" check therefore
    # always passed, regardless of intent. Fixed by moving kill state into
    # Redis (mirroring `halt_account`) and having `POST /orders` refresh
    # `stack.risk_engine.kill_switch` from it on every call -- this test
    # exercises exactly that path, going through the real Redis keys
    # `POST /admin/kill-switch/account/{id}` writes, not a manually-set
    # in-memory object.
    from app.core.redis import clear_account_kill, set_account_kill

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDKILL{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            await set_account_kill(str(user_id))

            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "trading is stopped" in r.text

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(select(Notification).where(Notification.user_id == user_id))
                ).scalars().all()
                assert any(n.type == NotificationType.ORDER_REJECTED for n in notifications)

            # Clearing it lets the account trade again.
            await clear_account_kill(str(user_id))
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text
        finally:
            await clear_account_kill(str(user_id))
            await _cleanup(user_id, instrument_id)


class _FakeRealBroker(Broker):
    """A minimal non-`MockBroker` double whose quote is fixed and
    independent of whatever `entry` a client submits -- unlike
    `MockBroker`, whose quote `place_order` always seeds from
    `payload.entry` by design. Stands in for a real connected broker
    (Upstox/Dhan) so `test_entry_far_from_the_real_broker_quote_is_rejected`
    below can exercise `entry_matches_market` end-to-end through
    `POST /orders` without live broker credentials."""

    def __init__(self, quote_price: float, balance: float = 100_000.0) -> None:
        self.quote_price = quote_price
        self.balance = balance

    async def get_account(self) -> AccountInfo:
        return AccountInfo(account_id="FAKE", balance=self.balance, equity=self.balance)

    async def get_positions(self) -> list[BrokerPosition]:
        return []

    async def get_orders(self) -> list[BrokerOrder]:
        return []

    async def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ltp=self.quote_price)

    async def place_order(self, request: OrderRequest) -> OrderResult:
        return OrderResult(
            broker_order_id=str(uuid.uuid4()),
            status=OrderStatus.FILLED,
            filled_quantity=request.quantity,
            average_fill_price=self.quote_price,
        )

    async def modify_order(self, broker_order_id: str, **changes) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        return OrderResult(broker_order_id=broker_order_id, status=OrderStatus.CANCELLED)

    async def is_healthy(self) -> bool:
        return True


async def test_entry_far_from_the_real_broker_quote_is_rejected(require_infra):
    # Regression test, end-to-end: `payload.entry` is otherwise trusted
    # input used both to size the position (calculate_position_size) and
    # to size that same position's notional risk checks (exposure_limit et
    # al., computed as quantity * entry) -- a client picking `entry` close
    # to `stop` inflates quantity while those notional checks, computed
    # from that same forged entry, still look small. `MockBroker` can't
    # exercise this (its quote is always seeded from `payload.entry`, by
    # design), so this uses `_FakeRealBroker`, whose quote is independent
    # of client input -- like a real connected broker.
    from app.api import orders as orders_module

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDDEV{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # First order builds this user's stack against the default
            # MockBroker, then swap in a broker with a fixed real quote.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            stack = orders_module._STACKS[user_id]
            stack.broker = _FakeRealBroker(quote_price=100.0)

            # The real quote is 100.0 -- claiming entry=150 with a tiny
            # stop distance would otherwise inflate quantity while looking
            # harmless in notional terms.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 150.0, "stop": 149.9},
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "Entry deviates" in r.text
        finally:
            await _cleanup(user_id, instrument_id)


async def test_closing_trade_records_the_real_fill_price_not_the_claimed_entry(require_infra):
    # Regression test: the `trades` journal row's `exit_price` was set to
    # `payload.entry` -- the client-supplied field -- even though `pnl` on
    # the very same row is computed from `PositionManager.apply_fill`'s
    # `price` parameter, which is `final_order.average_fill_price`, the
    # broker's actual fill. For a real broker those two values are
    # independent (that's the whole premise of `entry_matches_market`
    # above -- a real broker's quote doesn't move just because a client
    # typed a different `entry`), so any real fill produced an internally
    # inconsistent, permanently-persisted trades row: pnl/entry_price
    # reflected the true fill, exit_price didn't. Invisible with
    # `MockBroker` because its quote is always seeded from `payload.entry`
    # by design -- uses `_FakeRealBroker` (fixed, client-independent quote)
    # for the same reason `test_entry_far_from_the_real_broker_quote_is_
    # rejected` above needs it.
    from app.api import orders as orders_module

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDFILL{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # Opens a LONG position against the default MockBroker.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            # Swap in a real-broker stand-in whose quote (and therefore
            # fill price) is fixed at 101.0 -- the real market moved since
            # the position opened at 100.0, independent of whatever this
            # next order's client claims as `entry`. `ExecutionEngine`
            # captures its own `broker` reference at construction (see
            # `_UserTradingStack.__init__`), so it has to be swapped too --
            # otherwise the order would still actually execute against the
            # stale MockBroker even though `stack.broker.get_quote` (used
            # for the entry_matches_market check) sees the new one.
            stack = orders_module._STACKS[user_id]
            stack.broker = _FakeRealBroker(quote_price=101.0)
            stack.execution_engine.broker = stack.broker

            # A reducing SHORT fill: entry=101.5 is within the 1% deviation
            # tolerance of the real quote (101.0), so it passes
            # entry_matches_market -- but the actual fill still happens at
            # 101.0, not 101.5. A large stop distance keeps the sized
            # quantity small relative to the open position, so this only
            # reduces it (guaranteeing a record_trade call) without fully
            # closing or flipping it.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "SHORT", "entry": 101.5, "stop": 10.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                trade = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalar_one()
                assert float(trade.exit_price) == pytest.approx(101.0)
                assert float(trade.exit_price) != pytest.approx(101.5)
        finally:
            await _cleanup(user_id, instrument_id)


class _NeverFillsBroker(Broker):
    """A broker double whose orders never fill (stay `ACKNOWLEDGED`) and
    whose `cancel_order` always fails, standing in for a real broker
    reporting an ordinary cancel-time race (the order already filled or was
    already cancelled broker-side) -- `MockBroker`'s orders always fill
    immediately, so there'd otherwise be no way to reach `POST
    /orders/{id}/cancel` with a still-cancelable order at all."""

    async def get_account(self) -> AccountInfo:
        return AccountInfo(account_id="NEVERFILL", balance=100_000.0, equity=100_000.0)

    async def get_positions(self) -> list[BrokerPosition]:
        return []

    async def get_orders(self) -> list[BrokerOrder]:
        return []

    async def get_quote(self, symbol: str) -> Quote:
        return Quote(symbol=symbol, ltp=100.0)

    async def place_order(self, request: OrderRequest) -> OrderResult:
        return OrderResult(broker_order_id=str(uuid.uuid4()), status=OrderStatus.ACKNOWLEDGED, filled_quantity=0)

    async def modify_order(self, broker_order_id: str, **changes) -> OrderResult:
        raise NotImplementedError

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise BrokerError("Order already complete")

    async def is_healthy(self) -> bool:
        return True


async def test_cancel_order_broker_failure_is_surfaced_cleanly_and_leaves_status_unchanged(require_infra):
    # Regression test: `UpstoxBroker.cancel_order` had no try/except at
    # all -- an ordinary broker-level cancel failure (the order already
    # filled or was already cancelled in the meantime; not a bug, just a
    # race) propagated as a raw exception straight through `POST
    # /orders/{id}/cancel`, which had no try/except of its own either. That
    # both 500'd the request AND left the order permanently stuck: the
    # exception fired before `order_manager.transition(..., CANCELLED, ...)`
    # ever ran, so the order stayed at its prior status in both the
    # in-memory OrderManager and (since persist_order also never ran) the
    # database, forever, with no way for the client to tell what happened.
    from app.api import orders as orders_module

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"ORDCXL{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            # First order builds this user's stack against the default
            # MockBroker, then swap in a broker that never fills orders and
            # always fails to cancel them.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            stack = orders_module._STACKS[user_id]
            stack.broker = _NeverFillsBroker()
            stack.execution_engine.broker = stack.broker

            # Different stop from the first order above -- the idempotency
            # key (app/api/orders.py) is derived from
            # user/symbol/direction/entry/stop, so reusing the exact same
            # values here would return the *first* order unchanged (already
            # MONITORING against MockBroker) instead of placing a new one
            # against `_NeverFillsBroker`. Entry stays at 100.0, matching
            # `_NeverFillsBroker.get_quote`'s fixed 100.0 ltp, so this isn't
            # itself rejected by the entry_matches_market check.
            r = client.post(
                "/orders",
                json={"symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 94.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            order_id = r.json()["id"]
            assert r.json()["status"] == "ACKNOWLEDGED"

            r = client.post(f"/orders/{order_id}/cancel", headers=headers)
            assert r.status_code == 502, r.text
            assert "Order already complete" in r.text

            # The order must be left exactly as it was, never silently
            # marked CANCELLED and never vanished.
            r = client.get("/orders", headers=headers)
            assert r.status_code == 200, r.text
            order = next(o for o in r.json() if o["id"] == order_id)
            assert order["status"] == "ACKNOWLEDGED"
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_position_can_still_be_closed_after_the_daily_loss_limit_trips(require_infra):
    # Regression test: `RiskEngine.evaluate` was run on every order, and
    # every one of its exposure/loss/count checks is an *entry* check --
    # `max_open_positions` is literally phrased "N open vs limit M". So a
    # user who tripped the daily loss limit while still holding a losing
    # position was refused the one order that would end the loss, and left
    # holding it. `POST /orders` is the only way out: app/api/positions.py
    # and app/api/portfolio.py are read-only, and `POST /orders/{id}/cancel`
    # refuses anything already filled. The risk gate ran ~50 lines before
    # the request path even looked up `existing_position`, so it could not
    # have known the order was an exit.
    #
    # The existing daily-loss test cannot reach this state: it opens and
    # closes the *same* instrument, so the account is flat by the time the
    # limit trips, and its third order is a fresh entry.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            keeper = Instrument(
                symbol=f"KEEP{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            loser = Instrument(
                symbol=f"LOSE{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add_all([keeper, loser])
            await db.commit()
            await db.refresh(keeper)
            await db.refresh(loser)
            keeper_id, loser_id = keeper.id, loser.id

        try:
            # Two open longs, 100 shares each (0.5% of 100,000 over a 5-wide stop).
            for symbol in (keeper.symbol, loser.symbol):
                assert client.post(
                    "/orders",
                    json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                    headers=headers,
                ).status_code == 201

            # Close the loser at 50: a 5,000 realized loss, 5% of the
            # account against the 2% daily limit.
            assert client.post(
                "/orders",
                json={"symbol": loser.symbol, "direction": "SHORT", "entry": 50.0, "stop": 55.0},
                headers=headers,
            ).status_code == 201

            # The limit is now genuinely tripped -- a fresh entry is refused.
            entry = client.post(
                "/orders",
                json={"symbol": keeper.symbol, "direction": "LONG", "entry": 90.0, "stop": 85.0},
                headers=headers,
            )
            assert entry.status_code == 403, entry.text
            assert "Daily loss" in entry.text

            # ...but the order that CLOSES the remaining position must go
            # through. This returned 403 before the fix, leaving the user
            # holding a losing position with no way out until the UTC day
            # rolled over -- after the NSE session had closed.
            close = client.post(
                "/orders",
                json={"symbol": keeper.symbol, "direction": "SHORT", "entry": 99.0, "stop": 104.0},
                headers=headers,
            )
            assert close.status_code == 201, close.text

            async with async_session_factory() as db:
                still_open = (
                    await db.execute(
                        select(Position).where(Position.user_id == user_id, Position.is_open.is_(True))
                    )
                ).scalars().all()
            assert still_open == [], "the account must be flat once the closing order fills"
        finally:
            await _cleanup(user_id, keeper_id)
            async with async_session_factory() as db:
                await db.execute(delete(Instrument).where(Instrument.id == loser_id))
                await db.commit()


async def test_a_reducing_order_cannot_be_used_to_open_a_position(require_infra):
    # The security property that makes the exemption above safe. Skipping
    # the entry limits for an opposing order would otherwise be a way to
    # launder a fresh entry past every exposure, loss and count limit by
    # sending it as a larger opposing order, so the sized quantity is
    # clamped to what is actually open: such an order can only reduce or
    # flatten, never open or flip.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            keeper = Instrument(
                symbol=f"CLMP{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            loser = Instrument(
                symbol=f"CLML{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add_all([keeper, loser])
            await db.commit()
            await db.refresh(keeper)
            await db.refresh(loser)
            keeper_id, loser_id = keeper.id, loser.id

        try:
            for symbol in (keeper.symbol, loser.symbol):
                assert client.post(
                    "/orders",
                    json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                    headers=headers,
                ).status_code == 201
            assert client.post(
                "/orders",
                json={"symbol": loser.symbol, "direction": "SHORT", "entry": 50.0, "stop": 55.0},
                headers=headers,
            ).status_code == 201  # limit now tripped

            # A 1-wide stop sizes this at 500 units against an open 100 --
            # unclamped it would flatten the long and open a fresh 400-unit
            # short, having skipped every limit on the way through.
            oversized = client.post(
                "/orders",
                json={"symbol": keeper.symbol, "direction": "SHORT", "entry": 100.0, "stop": 101.0},
                headers=headers,
            )
            assert oversized.status_code == 201, oversized.text

            async with async_session_factory() as db:
                rows = (
                    await db.execute(
                        select(Position).where(Position.user_id == user_id, Position.instrument_id == keeper_id)
                    )
                ).scalars().all()
            assert len(rows) == 1
            # Flat, not short: the exemption reduced the position to zero
            # and stopped there.
            assert float(rows[0].quantity) == pytest.approx(0.0)
            assert rows[0].is_open is False
        finally:
            await _cleanup(user_id, keeper_id)
            async with async_session_factory() as db:
                await db.execute(delete(Instrument).where(Instrument.id == loser_id))
                await db.commit()


@pytest.mark.parametrize("order_type", ["LIMIT", "SL", "SL_M"])
async def test_orders_that_cannot_be_priced_are_refused_rather_than_filled_at_zero(order_type, require_infra):
    # Regression test: `PlaceOrderRequest.price` is optional for every
    # order type, and `MockBroker._resolve_fill_price` read it as
    # `request.price or 0.0` for anything that is not MARKET. A LIMIT
    # order carrying no limit price was therefore accepted (201, status
    # MONITORING) and filled at 0.0:
    #
    #   GET /positions -> [{quantity: 100.0, average_price: 0.0}]
    #   GET /portfolio -> {open_position_count: 1, total_exposure: 0.0}
    #   ...then closing it with an ordinary MARKET short at 100 booked
    #   GET /portfolio -> {total_realized_pnl: 10000.0}
    #
    # A zero fill is not a cheap fill. It reports a real open position as
    # zero exposure, feeds 0 into every later exposure_limit check, and
    # turns the close into a fabricated profit that also *loosens* the
    # daily and weekly loss budgets.
    #
    # SL/SL_M are refused outright: their trigger cannot be carried to the
    # broker (no `trigger_price` on this model, on OrderRecord, or in
    # ExecutionEngine.submit), so a real broker would receive a 0 trigger.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"NOPX{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            r = client.post(
                "/orders",
                json={
                    "symbol": instrument.symbol, "direction": "LONG",
                    "order_type": order_type, "entry": 100.0, "stop": 95.0,
                },
                headers=headers,
            )
            assert r.status_code == 422, r.text

            # Nothing may have been created by a refused order.
            async with async_session_factory() as db:
                positions = (
                    await db.execute(select(Position).where(Position.user_id == user_id))
                ).scalars().all()
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
            assert positions == []
            assert orders == []
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_limit_order_with_a_price_fills_at_that_price(require_infra):
    # The other half: LIMIT is not banned, it just has to say what price
    # it means. The fill must be that price, not the quote seeded from
    # `entry` -- so this also pins that the two are distinguishable.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"LIMP{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            r = client.post(
                "/orders",
                json={
                    "symbol": instrument.symbol, "direction": "LONG", "order_type": "LIMIT",
                    "entry": 100.0, "stop": 95.0, "price": 98.5,
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                position = (
                    await db.execute(select(Position).where(Position.user_id == user_id, Position.is_open.is_(True)))
                ).scalar_one()
                assert float(position.average_price) == pytest.approx(98.5)
                # ...and the size must be solved from the price it fills
                # at, not from `entry`. `quantity > 0` was the whole of
                # this assertion before, which is direction-free: it holds
                # just as well for the wrong number. On a 100,000 balance
                # at the default 0.5% risk per trade, 500 of risk over a
                # |98.5 - 95| = 3.5 stop distance is 142.857 units. Sizing
                # from `entry` instead gives |100 - 95| = 5 -> 100 units,
                # which carries 100 x 3.5 = 350 of real risk against a 500
                # budget -- the same disagreement that, at a wider gap,
                # becomes a limit bypass (see the test below).
                assert float(position.quantity) == pytest.approx(500 / 3.5)
                assert float(position.quantity) != pytest.approx(500 / 5)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_limit_order_is_sized_and_gated_on_the_price_it_will_fill_at(require_infra):
    # Regression test: sizing and every notional risk check read
    # `payload.entry` for all order types, while only MARKET actually
    # fills there. `payload.price` -- the thing that decides a LIMIT
    # order's fill -- was passed straight to the broker and compared to
    # nothing.
    #
    # Pre-fix, this order was sized as 500 / |100 - 95| = 100 units and
    # gated as 100 x 100 = 10,000 notional (10% of balance, against a 100%
    # exposure cap), then filled at 5,000 -- leaving the account holding
    # 500,000, i.e. 500% of its own balance, with 100 x |5000 - 95| =
    # 490,500 at risk on an order the engine approved as 0.5% risk. Its
    # RiskEvent row recorded every check green.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"LIMR{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            r = client.post(
                "/orders",
                json={
                    "symbol": instrument.symbol, "direction": "LONG", "order_type": "LIMIT",
                    "entry": 100.0, "stop": 95.0, "price": 5000.0,
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            quantity = r.json()["quantity"]

            # 500 of risk over a |5000 - 95| = 4905 stop distance.
            assert quantity == pytest.approx(500 / 4905)
            assert quantity < 1.0, "pre-fix this was 100 units"

            # The risk actually taken on is the configured 0.5%, not 490%.
            assert quantity * abs(5000.0 - 95.0) == pytest.approx(500.0)

            # And the exposure the account really carries is the exposure
            # the gate approved -- GET /portfolio reads `positions`, whose
            # `average_price` is the real fill.
            # `rel=1e-4` because `positions.quantity`/`average_price` are
            # Numeric columns, so the value read back is rounded relative
            # to the in-memory float -- 509.685 vs 509.684.
            portfolio = client.get("/portfolio", headers=headers).json()
            assert portfolio["total_exposure"] == pytest.approx(quantity * 5000.0, rel=1e-4)
            assert portfolio["total_exposure"] < portfolio["balance"], "pre-fix this was 500,000 vs a 100,000 balance"
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_short_limit_order_is_sized_on_its_limit_price_too(require_infra):
    # The same disagreement in the other direction, at a gap small enough
    # to look ordinary rather than adversarial. A short entered at 90 with
    # a stop at 105 risks 15 per unit, not the 5 that `entry=100` implies.
    # Pre-fix: sized 500 / 5 = 100 units carrying 100 x 15 = 1,500 of real
    # risk -- 1.5% of the account against a configured 0.5% cap, three
    # times over, with nothing anywhere recording that it happened.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"LIMS{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            r = client.post(
                "/orders",
                json={
                    "symbol": instrument.symbol, "direction": "SHORT", "order_type": "LIMIT",
                    "entry": 100.0, "stop": 105.0, "price": 90.0,
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            quantity = r.json()["quantity"]

            assert quantity == pytest.approx(500 / 15)
            assert quantity != pytest.approx(500 / 5), "pre-fix this was 100 units"
            assert quantity * abs(90.0 - 105.0) == pytest.approx(500.0)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_market_order_is_still_sized_on_entry(require_infra):
    # The fill price for a MARKET order is the market, which is what
    # `entry` claims to be and what `entry_matches_market` checks against
    # the broker's own quote. Nothing about that path changes.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"MKTS{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
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
            assert r.json()["quantity"] == pytest.approx(500 / 5)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_two_limit_orders_differing_only_in_price_are_not_deduped(require_infra):
    # The idempotency key was `{user}:{symbol}:{direction}:{entry}:{stop}`
    # -- no `price`. Now that the limit price decides both the fill and
    # the size, two orders differing only in it are two different orders;
    # without it in the key the second silently returned the first's fill
    # and no second order was ever placed.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"LIMD{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id = instrument.id

        try:
            base = {"symbol": instrument.symbol, "direction": "LONG", "order_type": "LIMIT", "entry": 100.0, "stop": 95.0}
            first = client.post("/orders", json={**base, "price": 99.0}, headers=headers)
            second = client.post("/orders", json={**base, "price": 98.0}, headers=headers)
            assert first.status_code == 201, first.text
            assert second.status_code == 201, second.text
            assert first.json()["id"] != second.json()["id"]
            assert first.json()["quantity"] == pytest.approx(500 / 4)
            assert second.json()["quantity"] == pytest.approx(500 / 3)
        finally:
            await _cleanup(user_id, instrument_id)


async def test_a_live_order_gets_a_broker_side_stop_that_actually_closes_it(require_infra):
    # Regression test: `payload.stop` was recorded on the position and
    # persisted, and then nothing in the system ever acted on it. The paper
    # engine checks a stop against each candle it is fed; a live position
    # has no candle loop and no watcher, so price could run straight
    # through a live stop with the position left open and losing. A real
    # desk answers this by resting a stop *at the broker*, which keeps
    # working while this process is restarting or disconnected.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"STOP{uuid.uuid4().hex[:6].upper()}", exchange="NSE",
                market=MarketType.EQUITY, instrument_type="EQ",
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id, symbol = instrument.id, instrument.symbol
        try:
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            stack = orders_api.all_stacks()[user_id]
            resting = [
                o for o in await stack.broker.get_orders()
                if o.order_type.value == "SL_M" and o.status == OrderStatus.ACKNOWLEDGED
            ]
            assert len(resting) == 1, "a live entry with a stop must leave a protective order resting"
            assert resting[0].quantity == pytest.approx(100.0)

            async with async_session_factory() as db:
                row = (
                    await db.execute(select(Position).where(Position.user_id == user_id))
                ).scalar_one()
                # Durable, because the order it names lives at the broker
                # and outlives this process.
                assert row.protective_order_id == resting[0].broker_order_id

            # The market gaps straight through the stop.
            stack.broker.set_quote(symbol, ltp=80.0)
            assert await stack.broker.get_positions() == []
        finally:
            await _cleanup(user_id, instrument_id)


async def test_closing_a_live_position_withdraws_its_protective_stop(require_infra):
    # A stop left resting against a position that no longer exists is worse
    # than useless: when it fires it opens a fresh naked position in the
    # opposite direction.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"STOP{uuid.uuid4().hex[:6].upper()}", exchange="NSE",
                market=MarketType.EQUITY, instrument_type="EQ",
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id, symbol = instrument.id, instrument.symbol
        try:
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            # Close it with an opposing order.
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "SHORT", "entry": 101.0, "stop": 106.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            stack = orders_api.all_stacks()[user_id]
            resting = [
                o for o in await stack.broker.get_orders()
                if o.order_type.value == "SL_M" and o.status == OrderStatus.ACKNOWLEDGED
            ]
            assert resting == [], "no stop may rest against a closed position"

            async with async_session_factory() as db:
                row = (
                    await db.execute(select(Position).where(Position.user_id == user_id))
                ).scalar_one()
                assert row.is_open is False
                assert row.protective_order_id is None
        finally:
            await _cleanup(user_id, instrument_id)


# --- a stop the broker would not take (blueprint §57, §60, §73-75) ---------
#
# `ensure_protective_stop` returned `str | None` and its one caller
# discarded the value, so a broker that refused the stop order produced an
# ERROR in a log file and nothing else: 201, MONITORING, a "position
# opened" notification, and a live position carrying the stop price it had
# been *sized from* with nothing at the broker that would ever act on it.


class _NoStopOrdersBroker(_MockBrokerBase):
    """A broker that takes the entry but not the stop. Routine in real
    life: a trigger too close to the last price, a freeze quantity, or
    stop orders not accepted for this segment right now."""

    def __init__(self, outcome: OrderStatus = OrderStatus.REJECTED) -> None:
        super().__init__()
        self._outcome = outcome

    async def place_order(self, request):
        if request.order_type.value in ("SL", "SL_M"):
            return OrderResult(
                broker_order_id="", status=self._outcome, rejection_reason="Stop orders not accepted right now"
            )
        return await super().place_order(request)


async def _instrument_for_stop_test() -> tuple[uuid.UUID, str]:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"NOSTOP{uuid.uuid4().hex[:6].upper()}", exchange="NSE",
            market=MarketType.EQUITY, instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id, instrument.symbol


async def test_a_refused_protective_stop_halts_the_account_and_says_so(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id, symbol = await _instrument_for_stop_test()
        stack = orders_api._UserTradingStack(_NoStopOrdersBroker())
        orders_api._STACKS[user_id] = stack
        try:
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            # The entry itself really did fill -- that is exactly why the
            # missing stop has to be said out loud rather than inferred
            # from a success response.
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["status"] == "MONITORING"

            # The behavioural assertions come first so a failure here
            # reads as "the account was left unprotected and nobody was
            # told", not as "a response field is missing".
            position = stack.position_manager.get(str(user_id), symbol)
            assert position.quantity == pytest.approx(100.0)
            assert position.protective_order_id is None
            assert [o for o in await stack.broker.get_orders() if o.order_type.value == "SL_M"] == []

            # Nothing further may be opened until a human has looked.
            assert await account_halt_reason(str(user_id)) is not None

            assert body["unprotected_reason"] is not None
            assert "no stop-loss at the broker" in body["unprotected_reason"]

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(
                        select(Notification).where(
                            Notification.user_id == user_id,
                            Notification.type == NotificationType.RECONCILIATION_REQUIRED,
                        )
                    )
                ).scalars().all()
                assert len(notifications) == 1
                assert notifications[0].data["fate_unknown"] is False
                audits = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.user_id == user_id, AuditLog.action == "order.protective_stop_unplaced"
                        )
                    )
                ).scalars().all()
                assert len(audits) == 1
        finally:
            await resume_account(str(user_id))
            orders_api._STACKS.pop(user_id, None)
            await _cleanup(user_id, instrument_id)


async def test_an_unanswered_protective_stop_records_the_fate_as_unknown(require_infra):
    """A broker that never answered may or may not have a stop resting.
    That is not the same as knowing there is none, and the record has to
    keep the two apart -- a reconciliation that assumes "bare" could place
    a second stop on top of a live one."""
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id, symbol = await _instrument_for_stop_test()
        stack = orders_api._UserTradingStack(_NoStopOrdersBroker(OrderStatus.FAILED))
        orders_api._STACKS[user_id] = stack
        try:
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert "unknown" in r.json()["unprotected_reason"]
            assert await account_halt_reason(str(user_id)) is not None

            async with async_session_factory() as db:
                notifications = (
                    await db.execute(
                        select(Notification).where(
                            Notification.user_id == user_id,
                            Notification.type == NotificationType.RECONCILIATION_REQUIRED,
                        )
                    )
                ).scalars().all()
                assert len(notifications) == 1
                assert notifications[0].data["fate_unknown"] is True
        finally:
            await resume_account(str(user_id))
            orders_api._STACKS.pop(user_id, None)
            await _cleanup(user_id, instrument_id)


async def test_a_placed_protective_stop_leaves_the_response_and_account_clean(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        instrument_id, symbol = await _instrument_for_stop_test()
        try:
            r = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            assert r.json()["unprotected_reason"] is None

            stack = orders_api.all_stacks()[user_id]
            assert stack.position_manager.get(str(user_id), symbol).protective_order_id is not None
            assert await account_halt_reason(str(user_id)) is None
        finally:
            await resume_account(str(user_id))
            orders_api._STACKS.pop(user_id, None)
            await _cleanup(user_id, instrument_id)
