"""Disconnecting a broker did not stop orders going through it.

`DELETE /brokers/{id}` marks the row DISCONNECTED and scrubs its stored
credentials -- explicitly because, in its own words, "a disconnect prompted
by a leaked or compromised token should not leave that token sitting in the
row". But `_stack_for` resolved the broker once, at first use, and the
adapter built from that account lives in process memory already holding
the token.

Measured end to end on a PAPER connection, which stamps its account id on
an order exactly as a real one does:

    order 1 while connected  -> 201, broker_account_id = X
    DELETE /brokers/X        -> 204, row DISCONNECTED, credentials gone
    order 2 after that       -> 201, broker_account_id = X, still X
    only after a restart     -> broker_account_id = None

So the single action a user has for "stop trading through this broker" did
not stop it, and with a real Upstox account those orders are real ones.
This is the shape rounds 92 and 110 each fixed once -- a revocation written
to a column no live reader re-reads -- and the remedy is theirs: check at
use, not only at build.

`_stack_for` is the one door: `/orders`, `/orders/{id}/cancel`,
`/options/execute`, `/positions` and `/portfolio` all go through it.
"""

import uuid

import pytest
from sqlalchemy import delete, select, update

from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS
from app.auth.security import TokenType, decode_token
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import (
    BrokerAccount,
    BrokerAccountStatus,
    TradingPermission,
    User,
    UserSession,
)
from app.database.session import async_session_factory
from app.main import app

import httpx


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _instrument() -> tuple[uuid.UUID, str]:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"BRK{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id, instrument.symbol


async def _account(client) -> tuple[dict, uuid.UUID]:
    email = f"brk-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "B"})
    assert r.status_code == 201, r.text
    token = (await client.post("/auth/login", json={"email": email, "password": "testpass123"})).json()["access_token"]
    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    async with async_session_factory() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(trading_permissions=[TradingPermission.LIVE_TRADE.value])
        )
        await db.commit()
    return {"Authorization": f"Bearer {token}"}, user_id


async def _connect_paper(client, headers) -> str:
    """A PAPER connection resolves to `MockBroker` *with its account id*,
    so it exercises the whole account-selection path -- which account is
    live, and which id gets stamped on an order -- without a real token or
    any network."""
    r = await client.post("/brokers/connect", json={"broker": "PAPER", "credentials": {}}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _order(symbol: str, entry: float = 100.0) -> dict:
    return {"symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": entry, "stop": entry * 0.95}


async def _broker_ids_for(user_id: uuid.UUID) -> list[str | None]:
    async with async_session_factory() as db:
        rows = (
            await db.execute(select(Order).where(Order.user_id == user_id).order_by(Order.created_at))
        ).scalars().all()
    return [str(row.broker_account_id) if row.broker_account_id else None for row in rows]


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first: `trades` references `positions`, `order_events`
    reference `orders`, and `orders` reference `broker_accounts`."""
    async with async_session_factory() as db:
        for user_id in user_ids:
            order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
            for order_id in order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
            for model in (Order, Trade, Position, Notification, RiskEvent, AuditLog, BrokerAccount, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        for instrument_id in instrument_ids:
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()
    for user_id in user_ids:
        _STACKS.pop(user_id, None)
        _TRADE_LOCKS.pop(user_id, None)
        _STACK_LOCKS.pop(user_id, None)


# --- the finding ----------------------------------------------------------


async def test_a_disconnected_broker_stops_receiving_orders_at_once(require_infra):
    """Behavioural proof, through the real endpoints. The second order must
    not carry the disconnected account, and must not need a restart to stop
    carrying it."""
    instrument_id, symbol = await _instrument()
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _account(client)
            user_ids.append(user_id)
            account_id = await _connect_paper(client, headers)

            assert (await client.post("/orders", json=_order(symbol), headers=headers)).status_code == 201
            assert await _broker_ids_for(user_id) == [account_id], "fixture: the account must execute while connected"

            assert (await client.delete(f"/brokers/{account_id}", headers=headers)).status_code == 204
            async with async_session_factory() as db:
                row = await db.get(BrokerAccount, uuid.UUID(account_id))
                assert row.status == BrokerAccountStatus.DISCONNECTED

            assert (await client.post("/orders", json=_order(symbol, 101.0), headers=headers)).status_code == 201
            assert await _broker_ids_for(user_id) == [account_id, None], (
                "the disconnected account was still executing orders"
            )
    finally:
        await _cleanup(user_ids, [instrument_id])


async def test_connecting_a_broker_takes_effect_without_a_restart_too(require_infra):
    """The same query, the other direction. A user who connects a broker
    mid-session -- or reconnects after a token expired, which creates a new
    row rather than reviving the old one -- should trade through it, not
    through whatever the process resolved at its first order."""
    instrument_id, symbol = await _instrument()
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _account(client)
            user_ids.append(user_id)

            # First order with nothing connected: MockBroker, no account.
            assert (await client.post("/orders", json=_order(symbol), headers=headers)).status_code == 201
            assert await _broker_ids_for(user_id) == [None]

            account_id = await _connect_paper(client, headers)
            assert (await client.post("/orders", json=_order(symbol, 101.0), headers=headers)).status_code == 201
            assert await _broker_ids_for(user_id) == [None, account_id], "the new connection did not take effect"

            # And a reconnect: a second, newer account wins, as
            # `resolve_broker` orders by created_at desc.
            newer = await _connect_paper(client, headers)
            assert newer != account_id
            assert (await client.post("/orders", json=_order(symbol, 102.0), headers=headers)).status_code == 201
            assert await _broker_ids_for(user_id) == [None, account_id, newer]
    finally:
        await _cleanup(user_ids, [instrument_id])


# --- what the fix must not break -----------------------------------------


async def test_an_unchanged_connection_keeps_the_same_stack(require_infra):
    """The control that makes this a targeted rebuild rather than a rebuild
    on every request. Discarding the stack each time would throw away the
    in-memory order and position state on every order -- correct only
    because the build path rehydrates it, and wasteful besides. Asserted on
    object identity, which no amount of equal-looking state can fake."""
    instrument_id, symbol = await _instrument()
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _account(client)
            user_ids.append(user_id)
            await _connect_paper(client, headers)

            assert (await client.post("/orders", json=_order(symbol), headers=headers)).status_code == 201
            first = _STACKS[user_id]
            assert (await client.post("/orders", json=_order(symbol, 101.0), headers=headers)).status_code == 201
            assert _STACKS[user_id] is first, "the stack was rebuilt although nothing about the connection changed"
    finally:
        await _cleanup(user_ids, [instrument_id])


async def test_the_rebuilt_stack_keeps_the_book_and_the_days_counters(require_infra):
    """A rebuild mid-session is only safe because the build path rehydrates
    from Postgres -- the position book (round 78), the order idempotency
    index (round 206) and the day's risk counters (round 154). If any of
    those stopped being rebuilt, this fix would quietly hand the account a
    fresh day's allowance every time it disconnected a broker."""
    instrument_id, symbol = await _instrument()
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _account(client)
            user_ids.append(user_id)
            account_id = await _connect_paper(client, headers)

            assert (await client.post("/orders", json=_order(symbol), headers=headers)).status_code == 201
            before = _STACKS[user_id]
            open_positions = len(before.position_manager.open_positions(str(user_id)))
            trades_today = before.trades_today
            assert open_positions == 1 and trades_today == 1, "fixture"

            assert (await client.delete(f"/brokers/{account_id}", headers=headers)).status_code == 204
            assert (await client.post("/orders", json=_order(symbol, 101.0), headers=headers)).status_code == 201

            after = _STACKS[user_id]
            assert after is not before, "fixture: the disconnect must have rebuilt the stack"
            assert len(after.position_manager.open_positions(str(user_id))) >= open_positions, (
                "the rebuilt stack lost the open position book"
            )
            assert after.trades_today == trades_today + 1, (
                f"the rebuilt stack lost the day's order count: {after.trades_today}"
            )
    finally:
        await _cleanup(user_ids, [instrument_id])


async def test_one_users_broker_change_does_not_rebuild_anothers_stack(require_infra):
    """Control. A check that looked at broker accounts without scoping them
    to the caller would rebuild every stack whenever anyone connected or
    disconnected anything."""
    instrument_id, symbol = await _instrument()
    user_ids = []
    try:
        async with _client() as client:
            quiet_headers, quiet_user = await _account(client)
            busy_headers, busy_user = await _account(client)
            user_ids += [quiet_user, busy_user]
            await _connect_paper(client, quiet_headers)

            assert (await client.post("/orders", json=_order(symbol), headers=quiet_headers)).status_code == 201
            quiet_stack = _STACKS[quiet_user]

            busy_account = await _connect_paper(client, busy_headers)
            assert (await client.delete(f"/brokers/{busy_account}", headers=busy_headers)).status_code == 204

            assert (await client.post("/orders", json=_order(symbol, 101.0), headers=quiet_headers)).status_code == 201
            assert _STACKS[quiet_user] is quiet_stack, "another account's broker change rebuilt this user's stack"
    finally:
        await _cleanup(user_ids, [instrument_id])


async def test_the_helper_agrees_with_what_resolve_broker_picks(require_infra):
    """`active_broker_account_id` exists so a caller can ask which account
    is live without building an adapter. Two rules for that would drift, so
    this pins them together on the cases that differ: none connected, one
    connected, two connected (newest wins), and all disconnected."""
    from app.trading.broker_resolver import active_broker_account_id, resolve_broker

    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _account(client)
            user_ids.append(user_id)

            async def both() -> tuple[uuid.UUID | None, uuid.UUID | None]:
                async with async_session_factory() as db:
                    user = await db.get(User, user_id)
                    _broker, resolved = await resolve_broker(db, user)
                    return await active_broker_account_id(db, user_id), resolved

            helper, resolved = await both()
            assert helper is resolved is None, "nothing connected: both say MockBroker"

            first = await _connect_paper(client, headers)
            helper, resolved = await both()
            assert str(helper) == str(resolved) == first

            second = await _connect_paper(client, headers)
            helper, resolved = await both()
            assert str(helper) == str(resolved) == second, "both must pick the newest active account"

            for account_id in (first, second):
                assert (await client.delete(f"/brokers/{account_id}", headers=headers)).status_code == 204
            helper, resolved = await both()
            assert helper is resolved is None, "all disconnected: both fall back to MockBroker"
    finally:
        await _cleanup(user_ids, [])


@pytest.mark.parametrize("route", ["/positions", "/portfolio"])
async def test_read_routes_sharing_the_stack_see_the_disconnect_too(require_infra, route):
    """`_stack_for` is the one door, so the read paths that resolve a stack
    get the same treatment. Asserted because a fix applied at the call site
    of `POST /orders` rather than inside `_stack_for` would leave these
    building their view from a broker the user has disconnected."""
    instrument_id, symbol = await _instrument()
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _account(client)
            user_ids.append(user_id)
            account_id = await _connect_paper(client, headers)

            assert (await client.post("/orders", json=_order(symbol), headers=headers)).status_code == 201
            assert _STACKS[user_id].broker_account_id == uuid.UUID(account_id)

            assert (await client.delete(f"/brokers/{account_id}", headers=headers)).status_code == 204
            assert (await client.get(route, headers=headers)).status_code == 200
            assert _STACKS[user_id].broker_account_id is None, (
                f"GET {route} kept a stack built from the disconnected account"
            )
    finally:
        await _cleanup(user_ids, [instrument_id])
