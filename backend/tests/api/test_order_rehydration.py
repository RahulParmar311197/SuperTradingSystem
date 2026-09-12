"""An identical order resubmitted after a restart must not fill twice.

`OrderManager`'s idempotency index is process memory. `persist_order` is
idempotent on `idempotency_key` and correctly assumes one key means one
order -- so a process that has forgotten its keys breaks that assumption:
`create_order` mints a second order, `persist_order` updates the *first*
order's row, and the second fill exists at the broker with no journal
entry for it.

Measured before the fix, resubmitting an identical order after clearing
`_STACKS`: the position went 100 -> 200 while the `orders` table still
held one row for 100.
"""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

from app.api import orders as orders_mod
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order as OrderRow
from app.database.models.trading import OrderEvent as OrderEventRow
from app.database.models.trading import Position as PositionRow
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.trading.persistence import ORDER_REHYDRATION_WINDOW, load_recent_orders

pytestmark = pytest.mark.asyncio

_ORDER = {"direction": "LONG", "order_type": "MARKET", "entry": 100.0, "stop": 95.0}


async def _register_and_grant(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"ordrehy-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "OrdRehy"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(
        "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers
    ).status_code == 200

    from app.auth.security import TokenType, decode_token

    return headers, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _make_instrument() -> str:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"ORH{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
            lot_size=1,
            tick_size=0.05,
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.symbol


async def _cleanup(user_id: uuid.UUID, symbol: str) -> None:
    async with async_session_factory() as db:
        order_ids = (
            await db.execute(select(OrderRow.id).where(OrderRow.user_id == user_id))
        ).scalars().all()
        for order_id in order_ids:
            await db.execute(delete(OrderEventRow).where(OrderEventRow.order_id == order_id))
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(OrderRow).where(OrderRow.user_id == user_id))
        await db.execute(delete(PositionRow).where(PositionRow.user_id == user_id))
        await db.execute(delete(RiskEvent).where(RiskEvent.user_id == user_id))
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.symbol == symbol))
        await db.commit()


def _restart_the_process() -> None:
    orders_mod._STACKS.clear()
    orders_mod._STACK_LOCKS.clear()


async def _counts(user_id: uuid.UUID) -> tuple[int, float]:
    async with async_session_factory() as db:
        orders = (
            await db.execute(select(func.count()).select_from(OrderRow).where(OrderRow.user_id == user_id))
        ).scalar_one()
        quantity = (
            await db.execute(
                select(func.coalesce(func.sum(PositionRow.quantity), 0)).where(
                    PositionRow.user_id == user_id, PositionRow.is_open.is_(True)
                )
            )
        ).scalar_one()
        return orders, float(quantity)


async def test_an_identical_resubmit_after_a_restart_does_not_fill_twice(require_infra):
    """Behavioural proof. The idempotency key is content-derived, so the
    same request is the same order -- and that must stay true across a
    restart, which is exactly when the in-memory index is lost."""
    symbol = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register_and_grant(client)
        try:
            first = client.post("/orders", json={"symbol": symbol, **_ORDER}, headers=headers)
            assert first.status_code == 201, first.text
            orders_before, quantity_before = await _counts(user_id)
            assert (orders_before, quantity_before) == (1, 100.0)

            _restart_the_process()

            again = client.post("/orders", json={"symbol": symbol, **_ORDER}, headers=headers)
            assert again.status_code == 201, again.text
            assert again.json()["id"] == first.json()["id"], (
                "the resubmit was treated as a new order after the restart"
            )

            orders_after, quantity_after = await _counts(user_id)
            assert quantity_after == 100.0, (
                f"the resubmit filled a second time: position is {quantity_after}, not 100"
            )
            assert orders_after == 1
        finally:
            _restart_the_process()
            await _cleanup(user_id, symbol)


async def test_a_restored_order_carries_all_of_its_events(require_infra):
    """Behavioural proof of a subtlety that would otherwise fail silently.

    `persist_order` appends `order.events[n:]`, where `n` is the count
    already in the database. An order restored with fewer events than the
    database holds would have subsequent transitions fall outside that
    slice and never be written -- the audit trail would stop at the
    restart with no error anywhere.

    Asserted at the loader rather than through a transition because
    `MockBroker` fills every order immediately, so there is no
    cancellable state to drive one through the API: `POST
    /orders/{id}/cancel` answers 409 on a MONITORING order by design.
    This pins the property the slice actually depends on.
    """
    symbol = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register_and_grant(client)
        try:
            placed = client.post("/orders", json={"symbol": symbol, **_ORDER}, headers=headers)
            assert placed.status_code == 201, placed.text
            order_id = uuid.UUID(placed.json()["id"])

            async with async_session_factory() as db:
                row = (
                    await db.execute(select(OrderRow).where(OrderRow.id == order_id))
                ).scalar_one()
                persisted_events = (
                    await db.execute(
                        select(func.count())
                        .select_from(OrderEventRow)
                        .where(OrderEventRow.order_id == order_id)
                    )
                ).scalar_one()
                restored = await load_recent_orders(
                    db,
                    user_id,
                    row.execution_mode,
                    since=datetime.now(timezone.utc) - ORDER_REHYDRATION_WINDOW,
                )

            assert persisted_events > 0, "the fixture produced no events, so this proves nothing"
            assert len(restored) == 1
            assert len(restored[0].events) == persisted_events, (
                f"restored {len(restored[0].events)} events but the database holds "
                f"{persisted_events}; persist_order would skip the difference forever"
            )
            assert restored[0].idempotency_key == row.idempotency_key
        finally:
            _restart_the_process()
            await _cleanup(user_id, symbol)


async def test_a_genuinely_different_order_still_goes_through_after_a_restart(require_infra):
    """Control. Rehydrating the index must not block real trading: an
    order differing in any part of the key is a different order and must
    still execute."""
    symbol = await _make_instrument()
    with TestClient(app) as client:
        headers, user_id = await _register_and_grant(client)
        try:
            first = client.post("/orders", json={"symbol": symbol, **_ORDER}, headers=headers)
            assert first.status_code == 201, first.text

            _restart_the_process()

            different = client.post(
                "/orders", json={"symbol": symbol, **{**_ORDER, "stop": 94.0}}, headers=headers
            )
            assert different.status_code == 201, different.text
            assert different.json()["id"] != first.json()["id"]

            orders_after, quantity_after = await _counts(user_id)
            assert orders_after == 2, "the second order was wrongly deduped onto the first"
            # Not an exact figure: a different stop is a different risk
            # distance, so the engine sizes the second order differently.
            # What matters is that it reached the book at all.
            assert quantity_after > 100.0, "the second, genuinely different order did not fill"
        finally:
            _restart_the_process()
            await _cleanup(user_id, symbol)
