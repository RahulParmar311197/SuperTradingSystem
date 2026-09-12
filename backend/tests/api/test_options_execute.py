import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

import app.api.orders as orders_module
from app.brokers.base import OrderResult
from app.brokers.mock import MockBroker
from app.core.redis import account_halt_reason, resume_account
from app.database.models.strategy import Direction
from app.core.encryption import encrypt_credentials
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification, NotificationType
from app.database.models.options import OptionChainSnapshot, OptionContract, OptionSnapshot
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, OrderStatus, Position, Trade
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User, UserSession
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
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
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
                    # No broker account is connected -> MockBroker -> must
                    # never be journaled as LIVE (blueprint §101).
                    assert order.execution_mode == ExecutionMode.PAPER

                positions = (await db.execute(select(Position).where(Position.user_id == user_id))).scalars().all()
                assert len(positions) == 2
                assert all(p.is_open for p in positions)
                assert all(p.execution_mode == ExecutionMode.PAPER for p in positions)
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_execute_notifies_on_trade_executed_and_position_closed(require_infra):
    # Regression test: `execute_options_strategy` only ever called
    # `create_notification` on risk rejection -- a real fill that opens or
    # closes a live position never notified at all, unlike
    # app/api/paper.py and app/workers/auto_trade_worker.py, which both
    # already fire TRADE_EXECUTED/POSITION_CLOSED for the identical event.
    # This endpoint places real orders through the same broker/risk/
    # persistence pipeline as POST /orders (its own docstring says so),
    # so it needs the exact same parity fix.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"NOTIF{uuid.uuid4().hex[:5].upper()}")

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

            async with async_session_factory() as db:
                notifications = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            assert len(notifications) == 2
            assert all(n.type == NotificationType.TRADE_EXECUTED for n in notifications)

            # Close both legs with the exact reversal (a bear call spread,
            # same defined-risk shape as the existing exit_price test) --
            # each closing leg must fire its own POSITION_CLOSED.
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread_close",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 125.0},
                        {"symbol": short_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 55.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                notifications = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            assert len(notifications) == 4
            by_type = {}
            for n in notifications:
                by_type[n.type] = by_type.get(n.type, 0) + 1
            assert by_type[NotificationType.TRADE_EXECUTED] == 2
            assert by_type[NotificationType.POSITION_CLOSED] == 2
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


async def test_execute_respects_account_kill_switch(require_infra):
    # Regression test: `evaluate_options_risk` (app/risk/options_risk.py)
    # already had a `kill_switch` parameter and checked it first, but
    # `POST /options/execute` never passed one -- it always got a fresh,
    # permanently-empty default (see app/risk/kill_switch.py), so this
    # path's kill-switch check was as dead as POST /orders's used to be.
    # Now refreshed from Redis on every call (see
    # app.risk.kill_switch.load_kill_switch_state), the same shared keys
    # `POST /admin/kill-switch/account/{id}` writes.
    from app.core.redis import clear_account_kill, set_account_kill

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"KILL{uuid.uuid4().hex[:5].upper()}")

        try:
            await set_account_kill(str(user_id))

            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 5.0},
                        {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 2.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "trading is stopped" in r.text

            async with async_session_factory() as db:
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
                assert orders == []  # nothing should have been placed
        finally:
            await clear_account_kill(str(user_id))
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_execute_respects_the_accounts_daily_loss_limit(require_infra):
    # Regression test: `evaluate_options_risk` (app/risk/options_risk.py)
    # never checked daily/weekly loss, open-position count, trades-per-day,
    # or repeated broker rejections at all -- unlike `RiskEngine.evaluate`
    # (app/risk/engine.py), which POST /orders, PaperTradingEngine, and
    # AutoTradeSupervisor all go through. A user whose equity trading had
    # already blown through RiskLimits.max_daily_loss_pct today could still
    # freely open options strategies through this endpoint, since
    # OptionsRiskProposal didn't even have fields to carry daily_pnl/
    # trades_today/open_positions/repeated_rejections into the decision.
    from app.api import orders as orders_module

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"DLOSS{uuid.uuid4().hex[:5].upper()}")

        async with async_session_factory() as db:
            equity_instrument = Instrument(
                symbol=f"DLOSSEQ{uuid.uuid4().hex[:5].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
            )
            db.add(equity_instrument)
            await db.commit()
            await db.refresh(equity_instrument)
            equity_instrument_id = equity_instrument.id

        try:
            # A real equity order builds this user's stack (see
            # app.api.orders._stack_for) -- fetch it and simulate a day
            # that already lost 3% of the account (default
            # RiskLimits.max_daily_loss_pct is 2.0%), the same way
            # test_orders.py's daily-loss-limit tests do.
            r = client.post(
                "/orders",
                json={"symbol": equity_instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            stack = orders_module._STACKS[user_id]
            stack.daily_pnl = -3000.0  # 3% of the 100k default MockBroker balance

            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 5.0},
                        {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 2.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "Daily loss" in r.text

            async with async_session_factory() as db:
                # Only the earlier equity order -- neither options leg was placed.
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
                assert len(orders) == 1
                assert orders[0].instrument_id == equity_instrument_id

                risk_events = (
                    await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))
                ).scalars().all()
                rejected = [e for e in risk_events if e.checks.get("daily_loss_limit") is False]
                assert len(rejected) == 1
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id, equity_instrument_id])


async def test_execute_risk_rejection_writes_audit_row_and_notifies(require_infra):
    # Regression test: unlike POST /orders (app/api/orders.py's
    # place_order), which always writes a RiskEvent audit row and fires an
    # ORDER_REJECTED notification on a risk rejection, execute_options_strategy
    # used to only raise the HTTPException -- no RiskEvent row for *any*
    # options risk decision (approved or rejected), and no notification.
    # GET /notifications never showed a blocked options strategy, and no
    # audit trail existed to reconstruct what happened.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"AUDIT{uuid.uuid4().hex[:5].upper()}")

        try:
            # Same oversized spread as the exposure-limit test above.
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
                risk_events = (await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))).scalars().all()
                assert len(risk_events) == 1
                assert risk_events[0].decision.value == "REJECT"

                notifications = (
                    await db.execute(select(Notification).where(Notification.user_id == user_id))
                ).scalars().all()
                assert len(notifications) == 1
                assert notifications[0].type == NotificationType.ORDER_REJECTED
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_execute_rejects_when_premium_deviates_from_real_quote(require_infra):
    # Regression test: `leg.premium` is otherwise trusted input that sizes
    # this strategy's own payoff/risk math (compute_payoff_summary),
    # unchecked against anything real -- the same shape of gap already
    # fixed for POST /orders's `entry` field (RiskEngine's
    # entry_matches_market), reopened here since that fix never touched
    # options execution. A real OptionSnapshot's bid/ask was already
    # fetched for the liquidity check and then discarded without ever
    # being compared to the claimed premium.
    from datetime import datetime, timezone

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"PDEV{uuid.uuid4().hex[:5].upper()}")

        async with async_session_factory() as db:
            chain = OptionChainSnapshot(
                underlying="NIFTY", expiry=long_leg.expiry, spot_price=25000.0, fetched_at=datetime.now(timezone.utc)
            )
            db.add(chain)
            await db.flush()
            contract = OptionContract(instrument_id=long_leg.id, chain_id=chain.id, strike=25000.0, option_type="CALL")
            db.add(contract)
            await db.flush()
            db.add(
                OptionSnapshot(
                    option_contract_id=contract.id,
                    bid=100.0,
                    ask=102.0,
                    ltp=101.0,
                    volume=1000,
                    open_interest=1000,
                    snapshot_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()
            contract_id = contract.id
            chain_id = chain.id

        try:
            # Real quote mid is 101.0 -- claiming premium=200.0 is a ~98%
            # deviation, well past the default 5% limit.
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "long_call",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 200.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "Premium deviates" in r.text

            async with async_session_factory() as db:
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
                assert orders == []  # nothing should have been placed
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(OptionSnapshot).where(OptionSnapshot.option_contract_id == contract_id))
                await db.execute(delete(OptionContract).where(OptionContract.id == contract_id))
                await db.execute(delete(OptionChainSnapshot).where(OptionChainSnapshot.id == chain_id))
                await db.commit()
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_execute_rejects_when_the_real_quote_is_stale(require_infra):
    # Regression test: `market_data_age_seconds` on `OptionsRiskProposal`
    # defaults to 0.0 and the only call site (this endpoint) never set it,
    # so `evaluate_options_risk`'s "market_data_fresh" check (age <= 10s)
    # could never fail -- it was structurally dead, and the persisted
    # RiskEvent audit row falsely recorded that freshness had been
    # verified for every approved strategy, stale snapshot or not.
    from datetime import datetime, timedelta, timezone

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"STALE{uuid.uuid4().hex[:5].upper()}")

        async with async_session_factory() as db:
            chain = OptionChainSnapshot(
                underlying="NIFTY", expiry=long_leg.expiry, spot_price=25000.0, fetched_at=datetime.now(timezone.utc)
            )
            db.add(chain)
            await db.flush()
            contract = OptionContract(instrument_id=long_leg.id, chain_id=chain.id, strike=25000.0, option_type="CALL")
            db.add(contract)
            await db.flush()
            db.add(
                OptionSnapshot(
                    option_contract_id=contract.id,
                    bid=100.0,
                    ask=102.0,
                    ltp=101.0,
                    volume=1000,
                    open_interest=1000,
                    # Past RiskLimits.market_data_max_staleness_seconds (10s
                    # default) but still under the liquidity filter's own,
                    # separate max_quote_age_seconds (30s default) -- this
                    # must fail on staleness specifically, not incidentally
                    # trip the (already-covered) liquidity check too.
                    snapshot_at=datetime.now(timezone.utc) - timedelta(seconds=15),
                )
            )
            await db.commit()
            contract_id = contract.id
            chain_id = chain.id

        try:
            # premium=101.0 matches the mid exactly, so this can only fail
            # on staleness, not on the (already-covered) premium-deviation check.
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "long_call",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 101.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 403, r.text
            assert "Data age" in r.text

            async with async_session_factory() as db:
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
                assert orders == []  # nothing should have been placed

                event = (await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))).scalar_one()
                assert event.checks["market_data_fresh"] is False
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(OptionSnapshot).where(OptionSnapshot.option_contract_id == contract_id))
                await db.execute(delete(OptionContract).where(OptionContract.id == contract_id))
                await db.execute(delete(OptionChainSnapshot).where(OptionChainSnapshot.id == chain_id))
                await db.commit()
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_execute_records_which_broker_account_executed_it(require_infra):
    # Regression test: `Order.broker_account_id` exists to trace a placed
    # order back to whichever connected BrokerAccount executed it (see
    # tests/api/test_orders.py::test_place_order_records_which_broker_account_executed_it,
    # which fixed this for POST /orders). execute_options_strategy places
    # real orders through that same broker/risk/persistence pipeline (its
    # own docstring says so) but never threaded broker_account_id into its
    # persist_order call, so every options leg ever executed -- including
    # through a real connected account -- was persisted with
    # broker_account_id always NULL. Uses an ACTIVE PAPER account
    # (resolves to MockBroker, same as the no-account default) so this
    # doesn't need real Upstox/Dhan credentials.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"OPTBA{uuid.uuid4().hex[:5].upper()}")

        async with async_session_factory() as db:
            account = BrokerAccount(
                user_id=user_id,
                broker=BrokerName.PAPER,
                encrypted_credentials=encrypt_credentials({}),
                status=BrokerAccountStatus.ACTIVE,
            )
            db.add(account)
            await db.commit()
            await db.refresh(account)
            account_id = account.id

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

            async with async_session_factory() as db:
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
                assert len(orders) == 2
                assert all(o.broker_account_id == account_id for o in orders)
        finally:
            async with async_session_factory() as db:
                # Orders reference broker_accounts (Order.broker_account_id),
                # so they must go first -- and OrderEvent rows must go
                # before their Order, same as _cleanup below does.
                order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
                for order_id in order_ids:
                    await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
                await db.execute(delete(Order).where(Order.user_id == user_id))
                await db.execute(delete(BrokerAccount).where(BrokerAccount.id == account_id))
                await db.commit()
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_closing_leg_records_the_real_fill_price_not_the_claimed_premium(require_infra):
    # Regression test: the `trades` journal row's `exit_price` was set to
    # `leg.premium` -- the client-supplied field -- even though `pnl` on
    # the same row is computed from `PositionManager.apply_fill`'s `price`
    # parameter, which is `final_order.average_fill_price`, the broker's
    # actual fill. Same bug and fix as
    # tests/api/test_orders.py::test_closing_trade_records_the_real_fill_price_not_the_claimed_entry,
    # in this endpoint's sibling code path. For a real broker those two
    # values are independent, so any real fill produced an internally
    # inconsistent trades row. Invisible with a zero-slippage MockBroker
    # because its quote is always seeded from `leg.premium` right before
    # the fill (line `stack.broker.set_quote(leg.symbol, ltp=leg.premium)`)
    # -- mutating the existing MockBroker's `slippage_pct` in place (no
    # need to swap the broker object itself, unlike test_orders.py's
    # `_FakeRealBroker`, since MockBroker's fill logic itself is what needs
    # to diverge from its own quote here) reproduces what any nonzero
    # spread/slippage on a real broker would do.
    from app.api import orders as orders_module

    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"FILL{uuid.uuid4().hex[:5].upper()}")

        try:
            # Opens a defined-risk bull call spread (same shape as
            # test_execute_bull_call_spread_persists_both_legs above).
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

            stack = orders_module._STACKS[user_id]
            stack.broker.slippage_pct = 2.0

            # Closes it with the exact reversal (a bear call spread, same
            # defined-risk shape, so this stays under the exposure limit
            # the way a naked single-leg reversal wouldn't). The quote for
            # long_leg gets seeded to the claimed 125.0, but the 2%
            # slippage now configured on the broker makes the actual SHORT
            # fill 122.5, not 125.0.
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_call_spread_close",
                    "legs": [
                        {"symbol": long_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 125.0},
                        {"symbol": short_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 55.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text

            async with async_session_factory() as db:
                trade = (
                    await db.execute(select(Trade).where(Trade.user_id == user_id, Trade.instrument_id == long_leg.id))
                ).scalar_one()
                assert float(trade.exit_price) == pytest.approx(122.5)
                assert float(trade.exit_price) != pytest.approx(125.0)
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_an_options_position_can_still_be_closed_after_the_daily_loss_limit_trips(require_infra):
    # Regression test: `evaluate_options_risk` ran unconditionally on every
    # order, and its exposure/loss/count checks are all *entry* checks --
    # `max_open_positions` is literally phrased "N open vs limit M". The
    # gate also ran well before the per-leg `existing_position` lookup in
    # the execution loop, so it could not have known the order was a close.
    # `POST /options/execute` is the only path that closes an options
    # position, so the holder of a losing spread was refused the one order
    # that would end the loss -- the same trap `POST /orders` had.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"OPTX{uuid.uuid4().hex[:4].upper()}")

        try:
            opened = client.post(
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
            assert opened.status_code == 201, opened.text

            # Drive the account past the daily loss limit by closing the
            # spread's short leg at a punishing premium... which is itself
            # a reducing order, so it must be allowed through.
            hit = client.post(
                "/options/execute",
                json={
                    "strategy_name": "close_short_leg",
                    "legs": [{"symbol": short_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 2050.0}],
                },
                headers=headers,
            )
            assert hit.status_code == 201, hit.text

            # The limit is now tripped: a fresh entry must be refused...
            entry = client.post(
                "/options/execute",
                json={
                    "strategy_name": "new_long_call",
                    "legs": [{"symbol": short_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 50.0}],
                },
                headers=headers,
            )
            assert entry.status_code == 403, entry.text

            # ...while the order closing the surviving long leg goes
            # through. This returned 403 before the fix.
            close = client.post(
                "/options/execute",
                json={
                    "strategy_name": "close_long_leg",
                    "legs": [{"symbol": long_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 110.0}],
                },
                headers=headers,
            )
            assert close.status_code == 201, close.text

            async with async_session_factory() as db:
                still_open = (
                    await db.execute(
                        select(Position).where(Position.user_id == user_id, Position.is_open.is_(True))
                    )
                ).scalars().all()
            assert still_open == [], "every leg must be closable once the limit trips"
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id])


async def test_a_reducing_options_order_cannot_open_a_leg(require_infra):
    # The security property behind the exemption. An oversized opposing
    # leg must be clamped to what is open, so an order that skipped the
    # entry limits can only flatten -- never open or flip a leg. And an
    # order mixing a genuine close with a fresh entry is not reducing at
    # all: it faces the full gate.
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        long_leg, short_leg = await _make_two_leg_instruments(f"OPTC{uuid.uuid4().hex[:4].upper()}")

        try:
            assert client.post(
                "/options/execute",
                json={
                    "strategy_name": "long_call",
                    "legs": [{"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 120.0}],
                },
                headers=headers,
            ).status_code == 201

            # 5 lots against 1 open lot: must flatten, not open a short.
            oversized = client.post(
                "/options/execute",
                json={
                    "strategy_name": "oversized_close",
                    "legs": [{"symbol": long_leg.symbol, "direction": "SHORT", "quantity": 5, "premium": 120.0}],
                },
                headers=headers,
            )
            assert oversized.status_code == 201, oversized.text

            async with async_session_factory() as db:
                rows = (
                    await db.execute(
                        select(Position).where(Position.user_id == user_id, Position.instrument_id == long_leg.id)
                    )
                ).scalars().all()
            assert len(rows) == 1
            assert float(rows[0].quantity) == pytest.approx(0.0)
            assert rows[0].is_open is False
        finally:
            await _cleanup(user_id, [long_leg.id, short_leg.id])


# --- Partial multi-leg execution (blueprint §37-40) -----------------------
#
# Each leg is a separate order and nothing at the exchange enforces the
# combination, so a strategy the risk engine approved on its *combined*
# payoff can end up half-established. Measured on a 25200/25000 bull put
# spread: the short put fills, the protective long put is rejected, and
# what was approved with a 6,500 max loss becomes a naked short put whose
# real max loss is 1,254,000 on a 100,000 account.


class _LegOutcomeBroker(MockBroker):
    """A MockBroker that gives one symbol a fixed non-fill outcome, the
    way a real broker rejects a single leg (insufficient margin, a freeze
    quantity, a contract that isn't tradable) or never answers one at all
    (see UpstoxBroker.place_order's FAILED result)."""

    def __init__(self, symbol: str, outcome: OrderStatus, reason: str) -> None:
        super().__init__()
        self._symbol = symbol
        self._outcome = outcome
        self._reason = reason

    async def place_order(self, request):
        if request.symbol == self._symbol:
            return OrderResult(broker_order_id="", status=self._outcome, rejection_reason=self._reason)
        return await super().place_order(request)


class _UnwindRejectingBroker(MockBroker):
    """Rejects one leg and then rejects the unwind order too -- a broker
    that has stopped accepting orders for this account mid-strategy."""

    def __init__(self, reject_symbol: str) -> None:
        super().__init__()
        self._reject_symbol = reject_symbol

    async def place_order(self, request):
        if request.symbol == self._reject_symbol or request.idempotency_key.startswith("unwind:"):
            return OrderResult(broker_order_id="", status=OrderStatus.REJECTED, rejection_reason="Broker refused")
        return await super().place_order(request)


def _install_stack(user_id: uuid.UUID, broker) -> object:
    """Puts a stack carrying `broker` where `_stack_for` will find it, so
    a leg outcome is chosen rather than waited for."""
    stack = orders_module._UserTradingStack(broker)
    orders_module._STACKS[user_id] = stack
    return stack


async def _bull_put_spread(client: TestClient, headers: dict, short_leg, long_leg) -> dict:
    r = client.post(
        "/options/execute",
        json={
            "strategy_name": "bull_put_spread",
            "legs": [
                {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 120.0},
                {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 50.0},
            ],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _make_put_spread_instruments(prefix: str) -> tuple[Instrument, Instrument]:
    """(short leg at 25200, protective long leg at 25000) -- a bull put
    spread, where losing the long leg is what leaves a naked short."""
    expiry = date.today() + timedelta(days=7)
    async with async_session_factory() as db:
        short_leg = Instrument(
            symbol=f"{prefix}25200PE", exchange="NSE", market=MarketType.OPTIONS, instrument_type="OPTION",
            underlying="NIFTY", expiry=expiry, strike=25200.0, option_type=OptionType.PUT, lot_size=50,
        )
        long_leg = Instrument(
            symbol=f"{prefix}25000PE", exchange="NSE", market=MarketType.OPTIONS, instrument_type="OPTION",
            underlying="NIFTY", expiry=expiry, strike=25000.0, option_type=OptionType.PUT, lot_size=50,
        )
        db.add_all([short_leg, long_leg])
        await db.commit()
        await db.refresh(short_leg)
        await db.refresh(long_leg)
        return short_leg, long_leg


async def test_rejected_protective_leg_is_unwound_and_the_account_is_halted(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        short_leg, long_leg = await _make_put_spread_instruments(f"PU{uuid.uuid4().hex[:5].upper()}")
        stack = _install_stack(user_id, _LegOutcomeBroker(long_leg.symbol, OrderStatus.REJECTED, "Insufficient margin"))

        try:
            body = await _bull_put_spread(client, headers, short_leg, long_leg)

            # The combination was approved on a bounded loss...
            assert body["max_loss"] == pytest.approx(-6500.0, rel=1e-3)
            # ...but only the short leg established.
            statuses = {leg["symbol"]: leg["status"] for leg in body["legs"]}
            assert statuses[short_leg.symbol] in ("FILLED", "MONITORING")
            assert statuses[long_leg.symbol] == "REJECTED"

            assert body["strategy_intact"] is False
            assert any("unwound" in line for line in body["remediation"])

            # The naked short put must not survive the call.
            assert stack.position_manager.get(str(user_id), short_leg.symbol).quantity == 0

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
                audits = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.user_id == user_id, AuditLog.action == "options_strategy.leg_unwound"
                        )
                    )
                ).scalars().all()
                assert len(audits) == 1
        finally:
            await resume_account(str(user_id))
            orders_module._STACKS.pop(user_id, None)
            await _cleanup(user_id, [short_leg.id, long_leg.id])


async def test_an_unanswered_leg_is_never_unwound(require_infra):
    """`FAILED` means the broker never answered -- the leg may or may not
    exist at the exchange. An opposing order against a maybe-position is
    not a reversal, it is a new position half the time, so the filled leg
    is deliberately left alone for reconciliation to resolve."""
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        short_leg, long_leg = await _make_put_spread_instruments(f"PF{uuid.uuid4().hex[:5].upper()}")
        stack = _install_stack(
            user_id, _LegOutcomeBroker(long_leg.symbol, OrderStatus.FAILED, "Upstox did not answer this order")
        )

        try:
            body = await _bull_put_spread(client, headers, short_leg, long_leg)

            statuses = {leg["symbol"]: leg["status"] for leg in body["legs"]}
            assert statuses[long_leg.symbol] == "FAILED"
            assert body["strategy_intact"] is False
            assert any("NOT unwound" in line for line in body["remediation"])

            # Still open, on purpose.
            assert stack.position_manager.get(str(user_id), short_leg.symbol).quantity == -50.0
            assert await account_halt_reason(str(user_id)) is not None

            async with async_session_factory() as db:
                unwinds = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.user_id == user_id, AuditLog.action == "options_strategy.leg_unwound"
                        )
                    )
                ).scalars().all()
                assert unwinds == []
        finally:
            await resume_account(str(user_id))
            orders_module._STACKS.pop(user_id, None)
            await _cleanup(user_id, [short_leg.id, long_leg.id])


async def test_a_fully_executed_strategy_is_intact_and_leaves_the_account_open(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        short_leg, long_leg = await _make_put_spread_instruments(f"PO{uuid.uuid4().hex[:5].upper()}")
        stack = _install_stack(user_id, MockBroker())

        try:
            body = await _bull_put_spread(client, headers, short_leg, long_leg)

            assert body["strategy_intact"] is True
            assert body["remediation"] == []
            assert stack.position_manager.get(str(user_id), short_leg.symbol).quantity == -50.0
            assert stack.position_manager.get(str(user_id), long_leg.symbol).quantity == 50.0
            assert await account_halt_reason(str(user_id)) is None
        finally:
            await resume_account(str(user_id))
            orders_module._STACKS.pop(user_id, None)
            await _cleanup(user_id, [short_leg.id, long_leg.id])


async def test_a_batch_where_no_leg_reached_the_exchange_does_not_halt(require_infra):
    """Nothing executed, so there is no imbalance -- halting here would
    lock an account out over an order the exchange never saw."""
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        short_leg, long_leg = await _make_put_spread_instruments(f"PN{uuid.uuid4().hex[:5].upper()}")
        broker = MockBroker()
        broker.reject_probability = 1.0
        stack = _install_stack(user_id, broker)

        try:
            body = await _bull_put_spread(client, headers, short_leg, long_leg)

            assert all(leg["status"] == "REJECTED" for leg in body["legs"])
            assert body["strategy_intact"] is False
            assert any("No leg reached the exchange" in line for line in body["remediation"])
            assert stack.position_manager.open_positions(str(user_id)) == []
            assert await account_halt_reason(str(user_id)) is None
        finally:
            await resume_account(str(user_id))
            orders_module._STACKS.pop(user_id, None)
            await _cleanup(user_id, [short_leg.id, long_leg.id])


async def test_an_unwind_the_broker_also_rejects_is_reported_not_hidden(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        short_leg, long_leg = await _make_put_spread_instruments(f"PX{uuid.uuid4().hex[:5].upper()}")
        stack = _install_stack(user_id, _UnwindRejectingBroker(long_leg.symbol))

        try:
            body = await _bull_put_spread(client, headers, short_leg, long_leg)

            assert body["strategy_intact"] is False
            assert any("still open" in line for line in body["remediation"])
            # The exposure really is still there -- the response must not
            # claim otherwise.
            assert stack.position_manager.get(str(user_id), short_leg.symbol).quantity == -50.0
            assert await account_halt_reason(str(user_id)) is not None
        finally:
            await resume_account(str(user_id))
            orders_module._STACKS.pop(user_id, None)
            await _cleanup(user_id, [short_leg.id, long_leg.id])


async def test_a_leg_that_flips_a_position_unwinds_only_the_new_exposure(require_infra):
    """The quantity to give back is what the position *gained*, not what
    the order filled. A fill that closes 50 long and opens 50 short
    opened 50, not 100 -- unwinding 100 would leave the account long
    again, opening a position out of a remediation."""
    with TestClient(app) as client:
        token, user_id = await _register_and_grant_live_trade(client)
        headers = {"Authorization": f"Bearer {token}"}
        short_leg, long_leg = await _make_put_spread_instruments(f"PZ{uuid.uuid4().hex[:5].upper()}")
        stack = _install_stack(user_id, _LegOutcomeBroker(long_leg.symbol, OrderStatus.REJECTED, "Insufficient margin"))
        # An existing long 50 in the leg this batch sells 100 of.
        stack.position_manager.apply_fill(str(user_id), short_leg.symbol, Direction.LONG, 50.0, 100.0)
        # Both legs 2 lots, so the combination stays the defined-risk
        # spread the exposure limit approves -- only the pre-existing
        # position makes one leg a flip.

        try:
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "bull_put_spread",
                    "legs": [
                        {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 2, "premium": 120.0},
                        {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 2, "premium": 50.0},
                    ],
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["strategy_intact"] is False

            # Flat: the 50 of new short exposure was given back, and the
            # pre-existing long was not re-opened by the unwind.
            assert stack.position_manager.get(str(user_id), short_leg.symbol).quantity == 0
        finally:
            await resume_account(str(user_id))
            orders_module._STACKS.pop(user_id, None)
            await _cleanup(user_id, [short_leg.id, long_leg.id])
