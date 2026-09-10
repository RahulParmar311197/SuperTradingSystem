"""Tests for `DELETE /brokers/{account_id}` (app/api/brokers.py).

The endpoint had no test coverage of any kind -- `grep "client.delete"`
over the suite hits only `/replay/{id}`, `/paper/{id}` and the admin
kill-switch keys. It hard-deleted the `broker_accounts` row, which worked
only while `orders.broker_account_id` was NULL for every row; once
`resolve_broker` began returning the account id so an order could be
traced back to the account that executed it, the FK (`ON DELETE NO
ACTION`) started refusing the delete and the uncaught `IntegrityError`
surfaced as a 500.

Note the suite's own cleanup helpers already delete orders before broker
accounts, so the ordering constraint was understood by the tests -- just
never asserted against the endpoint.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.core.encryption import decrypt_credentials
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import BrokerAccount, BrokerAccountStatus, User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register(client: TestClient, label: str) -> tuple[dict, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
    assert r.status_code == 200, r.text
    return headers, user_id


async def _cleanup(user_id: uuid.UUID, symbol: str | None = None) -> None:
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        for model in (Trade, Order, Position, RiskEvent, Notification, AuditLog, UserSession, BrokerAccount):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        if symbol is not None:
            await db.execute(delete(Instrument).where(Instrument.symbol == symbol))
        await db.commit()


async def _register_instrument(symbol: str) -> uuid.UUID:
    async with async_session_factory() as db:
        instrument = Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id


async def test_a_broker_account_that_has_placed_orders_can_still_be_disconnected(require_infra):
    # Regression test: this returned 500 for exactly the accounts that had
    # traded -- the ones a user most wants to revoke (a leaked token, or
    # switching brokers) -- because the order rows referencing the account
    # blocked the hard delete. `BrokerName.PAPER` stamps its id onto
    # orders too, so no real broker credentials are needed to reach it.
    symbol = f"BDIS{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "brokerdis")
        await _register_instrument(symbol)
        try:
            connected = client.post(
                "/brokers/connect",
                json={"broker": "PAPER", "credentials": {"access_token": "SECRET-TOKEN"}},
                headers=headers,
            )
            assert connected.status_code == 201, connected.text
            account_id = connected.json()["id"]

            placed = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert placed.status_code == 201, placed.text

            # The order really does reference this account -- otherwise
            # this test would pass without exercising the bug at all.
            async with async_session_factory() as db:
                stamped = (
                    await db.execute(select(Order.broker_account_id).where(Order.user_id == user_id))
                ).scalars().all()
            assert stamped == [uuid.UUID(account_id)]

            disconnected = client.delete(f"/brokers/{account_id}", headers=headers)
            assert disconnected.status_code == 204, disconnected.text
        finally:
            await _cleanup(user_id, symbol)


async def test_disconnecting_marks_the_account_and_scrubs_its_credentials(require_infra):
    # The row is kept so `orders.broker_account_id` still resolves, but the
    # stored credentials are cleared: the old hard delete removed them as a
    # side effect, and a disconnect prompted by a compromised token must
    # not leave that token in the row.
    with TestClient(app) as client:
        headers, user_id = await _register(client, "brokerscrub")
        try:
            account_id = client.post(
                "/brokers/connect",
                json={"broker": "PAPER", "credentials": {"access_token": "SECRET-TOKEN"}},
                headers=headers,
            ).json()["id"]

            assert client.delete(f"/brokers/{account_id}", headers=headers).status_code == 204

            listed = client.get("/brokers", headers=headers).json()
            assert [(row["broker"], row["status"]) for row in listed] == [("PAPER", "DISCONNECTED")]

            async with async_session_factory() as db:
                account = await db.get(BrokerAccount, uuid.UUID(account_id))
                assert account is not None, "the row must survive so the order audit trail still resolves"
                assert account.status is BrokerAccountStatus.DISCONNECTED
                assert decrypt_credentials(account.encrypted_credentials) == {}
        finally:
            await _cleanup(user_id)


async def test_disconnecting_twice_is_idempotent(require_infra):
    with TestClient(app) as client:
        headers, user_id = await _register(client, "brokertwice")
        try:
            account_id = client.post(
                "/brokers/connect", json={"broker": "PAPER", "credentials": {}}, headers=headers
            ).json()["id"]
            assert client.delete(f"/brokers/{account_id}", headers=headers).status_code == 204
            assert client.delete(f"/brokers/{account_id}", headers=headers).status_code == 204
        finally:
            await _cleanup(user_id)


async def test_a_user_cannot_disconnect_another_users_broker_account(require_infra):
    with TestClient(app) as client:
        owner_headers, owner_id = await _register(client, "brokerowner")
        other_headers, other_id = await _register(client, "brokerother")
        try:
            account_id = client.post(
                "/brokers/connect", json={"broker": "PAPER", "credentials": {}}, headers=owner_headers
            ).json()["id"]

            assert client.delete(f"/brokers/{account_id}", headers=other_headers).status_code == 404

            async with async_session_factory() as db:
                account = await db.get(BrokerAccount, uuid.UUID(account_id))
                assert account.status is BrokerAccountStatus.ACTIVE
        finally:
            await _cleanup(owner_id)
            await _cleanup(other_id)
