import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.encryption import encrypt_credentials
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification, NotificationType
from app.database.models.options import OptionChainSnapshot, OptionContract, OptionSnapshot
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position, Trade
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
