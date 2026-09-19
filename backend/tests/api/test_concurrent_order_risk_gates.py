"""Concurrent orders walked straight through every entry-side risk gate.

`RiskEngine.evaluate` is check-then-act, and nothing serialized it.
`POST /orders` reads `open_positions`, `current_exposure`, `trades_today`
and the daily/weekly P&L, then awaits its way through the broker quote,
the liquidity read and the kill-switch load before evaluating and filling.
On a single event loop another request for the same user runs inside every
one of those awaits, reads the same pre-fill state, and reaches the same
verdict.

Round 58 added `_STACK_LOCKS`, but that lock only guards *building* the
stack; it is released the moment the stack exists, which is before
anything that enforces a limit.

Measured against the real ASGI app with `asyncio.gather` -- one user, ten
orders on ten distinct symbols, `max_open_positions=5`:

    sequential:  201 201 201 201 201 403 403 403 403 403  -> 5 positions
    concurrent:  201 201 201 201 201 201 201 201 201 201  -> 10 positions

Not "approximately 5 under load" -- the limit was simply absent, and ten
positions opened against a cap of five. The same mechanism defeats
`max_trades_per_day`, `exposure_limit` and the loss limits, and
`POST /options/execute` shares the same per-user stack.

`asyncio.gather` on one event loop is not an artificial stress: it is
exactly how uvicorn serves two concurrent requests. A user double-clicking
a button, a mobile client retrying, or two devices acting at once all
produce it.
"""

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import delete, select, update

from app.api.orders import _STACKS, _TRADE_LOCKS
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app

# `RiskLimits.max_open_positions`. Read from the dataclass rather than
# hard-coded so this file cannot silently drift from the limit it tests.
from app.risk.limits import RiskLimits

MAX_OPEN = RiskLimits().max_open_positions
ORDERS = MAX_OPEN * 2


# Every concurrent wait in this file goes through here. Injecting a lock
# that is acquired and never released made an unbounded `asyncio.gather`
# hang for the full 600s CI budget instead of failing -- a control that
# hangs reports nothing. With a deadline the same injection fails in 30s
# and names itself.
_DEADLINE_SECONDS = 30


async def _gather_bounded(*awaitables):
    try:
        return await asyncio.wait_for(asyncio.gather(*awaitables), timeout=_DEADLINE_SECONDS)
    except asyncio.TimeoutError:  # pragma: no cover - only on a regression
        raise AssertionError(
            f"{len(awaitables)} concurrent orders did not all finish within {_DEADLINE_SECONDS}s. "
            "The usual cause is a per-user lock acquired and never released, which wedges that "
            "account's trading permanently."
        ) from None


async def _post(client, url, *, json, headers):
    """Every single request in this file, sequential ones included.

    Bounding only the `gather` calls was not enough: injecting a lock that
    is acquired and never released left the *sequential* awaits hanging
    with no deadline at all, and the run had to be killed rather than
    reporting anything. A control that hangs reports nothing, so there is
    no unbounded await left in this module.
    """
    try:
        return await asyncio.wait_for(client.post(url, json=json, headers=headers), timeout=_DEADLINE_SECONDS)
    except asyncio.TimeoutError:  # pragma: no cover - only on a regression
        raise AssertionError(
            f"POST {url} did not finish within {_DEADLINE_SECONDS}s -- most likely a per-user "
            "lock acquired and never released."
        ) from None


async def _make_instruments(count: int) -> tuple[list[uuid.UUID], list[str]]:
    ids, symbols = [], []
    async with async_session_factory() as db:
        for _ in range(count):
            instrument = Instrument(
                symbol=f"CNC{uuid.uuid4().hex[:6].upper()}", exchange="NSE",
                market=MarketType.EQUITY, instrument_type="EQ",
            )
            db.add(instrument)
            await db.flush()
            ids.append(instrument.id)
            symbols.append(instrument.symbol)
        await db.commit()
    return ids, symbols


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first: order_events before orders, then everything that
    references the user, then the user, then the instruments."""
    async with async_session_factory() as db:
        for user_id in user_ids:
            order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
            for order_id in order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
            for model in (Order, Position, Trade, Notification, RiskEvent, AuditLog, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        for instrument_id in instrument_ids:
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()
    # The in-memory stack and its lock outlive the request; a test that
    # left them behind would hand the next test this user's positions.
    for user_id in user_ids:
        _STACKS.pop(user_id, None)
        _TRADE_LOCKS.pop(user_id, None)


async def _register(client: httpx.AsyncClient) -> tuple[dict, uuid.UUID]:
    email = f"conc-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "C"})
    assert r.status_code == 201, r.text
    token = (await client.post("/auth/login", json={"email": email, "password": "testpass123"})).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    async with async_session_factory() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(trading_permissions=[TradingPermission.LIVE_TRADE.value])
        )
        await db.commit()
    return {"Authorization": f"Bearer {token}"}, user_id


def _order_body(symbol: str) -> dict:
    return {"symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": 100.0, "stop": 95.0}


async def _open_position_count(user_id: uuid.UUID) -> int:
    async with async_session_factory() as db:
        rows = (
            await db.execute(select(Position).where(Position.user_id == user_id, Position.is_open.is_(True)))
        ).scalars().all()
    return len(rows)


async def _client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- the finding ----------------------------------------------------------


async def test_concurrent_orders_cannot_exceed_max_open_positions(require_infra):
    """Behavioural proof. Ten orders fired together on one event loop --
    which is exactly how uvicorn serves two concurrent requests, not a
    synthetic stress. Before the lock all ten filled."""
    instrument_ids, symbols = await _make_instruments(ORDERS)
    user_ids = []
    try:
        async with await _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            responses = await _gather_bounded(
                *(_post(client, "/orders", json=_order_body(s), headers=headers) for s in symbols)
            )
            codes = [r.status_code for r in responses]

            assert codes.count(201) == MAX_OPEN, f"{codes.count(201)} orders filled against a limit of {MAX_OPEN}: {codes}"
            assert codes.count(403) == ORDERS - MAX_OPEN, codes
            assert await _open_position_count(user_id) == MAX_OPEN
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_the_sequential_answer_and_the_concurrent_answer_agree(require_infra):
    """Behavioural proof that the fix restores the *intended* semantics,
    not merely some smaller number. Two users, identical instruments and
    identical orders; one drives them one at a time, the other all at
    once. The two accounts must end up in the same state."""
    instrument_ids, symbols = await _make_instruments(ORDERS)
    user_ids = []
    try:
        async with await _client() as client:
            seq_headers, seq_user = await _register(client)
            con_headers, con_user = await _register(client)
            user_ids += [seq_user, con_user]

            sequential = [(await _post(client, "/orders", json=_order_body(s), headers=seq_headers)).status_code
                          for s in symbols]
            concurrent = [r.status_code for r in await _gather_bounded(
                *(_post(client, "/orders", json=_order_body(s), headers=con_headers) for s in symbols)
            )]

            assert sorted(sequential) == sorted(concurrent), (sequential, concurrent)
            assert await _open_position_count(seq_user) == await _open_position_count(con_user) == MAX_OPEN
    finally:
        await _cleanup(user_ids, instrument_ids)


# --- what the lock must not break ----------------------------------------


async def test_a_rejected_order_still_releases_the_lock(require_infra):
    """Control, and the failure mode the FIX could introduce, which would
    be worse than the bug: a lock held past a rejection would wedge that
    account's trading forever.

    Bounded with `asyncio.wait_for` so that a lock never released FAILS
    rather than hanging the suite.
    """
    instrument_ids, symbols = await _make_instruments(ORDERS + 1)
    user_ids = []
    try:
        async with await _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            for symbol in symbols[:MAX_OPEN]:
                assert (await _post(client, "/orders", json=_order_body(symbol), headers=headers)).status_code == 201

            rejected = await _post(client, "/orders", json=_order_body(symbols[MAX_OPEN]), headers=headers)
            assert rejected.status_code == 403, rejected.text

            after = await _post(client, "/orders", json=_order_body(symbols[MAX_OPEN + 1]), headers=headers)
            assert after.status_code == 403, after.text
            assert not _TRADE_LOCKS[user_id].locked(), "the lock is still held after the handler returned"
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_422_before_the_handler_body_still_releases_the_lock(require_infra):
    """Control. A request rejected by pydantic never reaches the handler,
    but the dependency has already been entered -- so this is the path
    where an `async with` in the handler body would have been enough and
    a dependency must still be proven to unwind."""
    instrument_ids, symbols = await _make_instruments(1)
    user_ids = []
    try:
        async with await _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            bad = await _post(
                client, "/orders",
                json={"symbol": symbols[0], "direction": "LONG", "entry": -1.0, "stop": 95.0},
                headers=headers,
            )
            assert bad.status_code == 422, bad.text

            good = await _post(client, "/orders", json=_order_body(symbols[0]), headers=headers)
            assert good.status_code == 201, good.text
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_two_users_are_not_serialized_against_each_other(require_infra):
    """Control. The lock is per user: one account's queue must not block
    another's, and each must still get its own full allowance. A single
    global lock would pass every proof above and quietly halve throughput
    for everyone."""
    instrument_ids, symbols = await _make_instruments(MAX_OPEN)
    user_ids = []
    try:
        async with await _client() as client:
            a_headers, a_user = await _register(client)
            b_headers, b_user = await _register(client)
            user_ids += [a_user, b_user]

            responses = await _gather_bounded(
                *(_post(client, "/orders", json=_order_body(s), headers=a_headers) for s in symbols),
                *(_post(client, "/orders", json=_order_body(s), headers=b_headers) for s in symbols),
            )
            assert [r.status_code for r in responses] == [201] * (MAX_OPEN * 2), [r.status_code for r in responses]
            assert await _open_position_count(a_user) == MAX_OPEN
            assert await _open_position_count(b_user) == MAX_OPEN
            assert a_user in _TRADE_LOCKS and b_user in _TRADE_LOCKS
            assert _TRADE_LOCKS[a_user] is not _TRADE_LOCKS[b_user], "one lock shared by two users"
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_one_ordinary_order_is_unchanged(require_infra):
    """Control with the numbers pinned. A lock that serialized correctly
    but broke the ordinary path would pass every count above."""
    instrument_ids, symbols = await _make_instruments(1)
    user_ids = []
    try:
        async with await _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            r = await _post(client, "/orders", json=_order_body(symbols[0]), headers=headers)
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["symbol"] == symbols[0]
            assert body["direction"] == "LONG"
            assert body["quantity"] > 0, body
            assert await _open_position_count(user_id) == 1
    finally:
        await _cleanup(user_ids, instrument_ids)


@pytest.mark.parametrize("repeat", range(3))
async def test_the_concurrent_result_is_not_a_lucky_interleaving(repeat, require_infra):
    """Control against a flaky proof. Async interleaving is scheduler
    dependent, so the headline test is repeated: if the lock were absent
    the over-fill reproduced on every single run, and if it were somehow
    timing dependent this is where that would show."""
    instrument_ids, symbols = await _make_instruments(ORDERS)
    user_ids = []
    try:
        async with await _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)
            responses = await _gather_bounded(
                *(_post(client, "/orders", json=_order_body(s), headers=headers) for s in symbols)
            )
            assert [r.status_code for r in responses].count(201) == MAX_OPEN
            assert await _open_position_count(user_id) == MAX_OPEN
    finally:
        await _cleanup(user_ids, instrument_ids)


# --- the other route that shares this stack ------------------------------


@pytest.mark.parametrize("path", ["/orders", "/options/execute"])
def test_every_route_that_trades_the_shared_stack_takes_the_lock(path):
    """Structural, and labelled as such: it asserts the wiring, not the
    behaviour.

    `POST /options/execute` resolves the same per-user stack through
    `_stack_for`, so it races exactly the way `POST /orders` did. Proving
    that behaviourally needs registered option contracts, fresh
    `option_snapshots` rows and a multi-leg payload -- machinery this file
    does not build. The mechanism itself is proven behaviourally on
    `/orders` above; what this adds is that the options route is actually
    wired to the same mechanism, which is the part that would silently
    rot if someone added a third trading route.

    Non-vacuous: deleting the dependency from either route fails it.
    """
    from app.api.orders import serialize_user_trading
    from app.main import app as fastapi_app

    routes = [
        r for r in fastapi_app.routes
        if getattr(r, "path", None) == path and "POST" in getattr(r, "methods", set())
    ]
    assert routes, f"no POST {path} route found"
    dependencies = routes[0].dependant.dependencies
    assert any(d.call is serialize_user_trading for d in dependencies), (
        f"POST {path} trades the shared per-user stack but does not take the per-user lock; "
        f"its dependencies are {[d.call.__name__ for d in dependencies]}"
    )
