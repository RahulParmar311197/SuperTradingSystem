"""Two concurrent cancels of one order escaped as a 500.

`POST /orders/{id}/cancel` is check-then-act across an await:

    order = stack.order_manager.get(order_id)          # reads status
    if order.status not in (SUBMITTED, ACKNOWLEDGED): 409
    await stack.broker.cancel_order(...)               # network round trip
    stack.order_manager.transition(order_id, CANCELLED)

`place_order` has taken the per-user `serialize_user_trading` lock since
round 152; this route never did. So two concurrent cancels both passed the
status guard, both cancelled at the broker, and the second reached
`transition` on an order already CANCELLED. `_ALLOWED_TRANSITIONS[CANCELLED]`
is the empty set, so that raised `IllegalTransitionError` out of the
handler. Measured against the real ASGI app, one resting ACKNOWLEDGED
order:

    cancel 1: 200
    cancel 2: IllegalTransitionError: Cannot move order from CANCELLED
              to CANCELLED

Round 87 (PR #100) fixed the SEQUENTIAL 500 on this route. This is the
concurrent one.

MockBroker's `cancel_order` RETURNS on an already-terminal order rather
than raising, so no `BrokerError`/502 absorbs the second request on the
way to the transition.

WHY THE INTERLEAVE IS FORCED WITH AN EVENT, NOT A SLEEP. The first two
runs of the original probe reported no bug: a broker whose `cancel_order`
has no real await never yields, so request 1 ran the whole critical
section atomically and request 2 saw a clean 409. A 0.05s sleep still did
not reproduce it; 2.0s did. A race that depends on a sleep being long
enough is a flaky test on someone else's CI, so `_GatedBroker` below
blocks on an `asyncio.Event` the test releases itself: the window is
opened deliberately, and the test is deterministic in both the fixed and
the broken world.
"""

import asyncio
import ast
import pathlib
import uuid

import httpx
from sqlalchemy import delete, select

from app.api import orders as orders_module
from app.auth.security import hash_password
from app.brokers.base import BrokerError, OrderResult
from app.brokers.mock import MockBroker
from app.core.redis import set_latest_price
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, OrderStatus, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app

# Every wait in this file is bounded. A control that hangs reports nothing,
# and the regression this file guards is exactly the kind that can wedge a
# per-user lock forever.
_DEADLINE = 30


class _RestingBroker(MockBroker):
    """Never fills, so the order stays cancellable, and cancels cleanly."""

    async def place_order(self, request):
        result = await super().place_order(request)
        return OrderResult(result.broker_order_id, OrderStatus.ACKNOWLEDGED, 0.0, None)

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        return OrderResult(broker_order_id, OrderStatus.CANCELLED, 0.0, None)


class _GatedBroker(_RestingBroker):
    """Holds the FIRST cancel inside the broker call until released.

    This is the window a real broker opens by doing network I/O, made
    deterministic: `entered` fires once a cancel is inside, and the call
    does not return until the test sets `release`.
    """

    def __init__(self) -> None:
        super().__init__()
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancels = 0

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        self.cancels += 1
        if self.cancels == 1:
            self.entered.set()
            await self.release.wait()
        return OrderResult(broker_order_id, OrderStatus.CANCELLED, 0.0, None)


class _CancelAlwaysFailsBroker(_RestingBroker):
    """A broker that refuses every cancel -- the 502 path, used to prove the
    lock is released when the handler raises."""

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise BrokerError("Order already complete")


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _user(client) -> tuple[uuid.UUID, dict]:
    email = f"cxl-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"),
                   name="Cancel Race", trading_permissions=[TradingPermission.LIVE_TRADE.value])
        db.add(row)
        await db.commit()
        user_id = row.id
    r = await client.post("/auth/login", json={"email": email, "password": "testpass123"})
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _instrument() -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(symbol=f"CXL{uuid.uuid4().hex[:6].upper()}", exchange="NSE",
                         market=MarketType.EQUITY, instrument_type="EQ")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _resting_order(client, headers, user_id, instrument, broker) -> str:
    """Build the user's stack, swap in `broker`, and leave one order resting."""
    r = await client.post("/orders", headers=headers, json={
        "symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0})
    assert r.status_code == 201, r.text

    stack = orders_module._STACKS[user_id]
    stack.broker = broker
    stack.execution_engine.broker = broker
    # A connected broker in a real deployment has a live feed behind it, and
    # `market_data_fresh` treats "no feed at all" as unfresh for a
    # non-MockBroker stack. This file is about cancelling, not a dead feed.
    await set_latest_price(instrument.symbol, 100.0)

    # A different stop: the idempotency key is derived from
    # user/symbol/direction/entry/stop, so reusing the first order's values
    # would return that order instead of placing a new one.
    r = await client.post("/orders", headers=headers, json={
        "symbol": instrument.symbol, "direction": "LONG", "entry": 100.0, "stop": 94.0})
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "ACKNOWLEDGED", "the fixture must leave a cancellable order"
    return r.json()["id"]


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        for model in (TradeRow, Order, Position, Notification, RiskEvent, AuditLog, UserSession):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()
    for cache in (orders_module._STACKS, orders_module._STACK_LOCKS, orders_module._TRADE_LOCKS):
        cache.pop(user_id, None)


# --- the finding ----------------------------------------------------------


async def test_two_concurrent_cancels_do_not_escape_as_a_500(require_infra):
    """The headline, through the real ASGI app with the window forced open."""
    instrument = await _instrument()
    broker = _GatedBroker()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            order_id = await _resting_order(client, headers, user_id, instrument, broker)

            first = asyncio.create_task(client.post(f"/orders/{order_id}/cancel", headers=headers))
            # Only start the second once the first is provably inside the
            # broker call, i.e. past the status guard and before the
            # transition. This is the window, opened deliberately.
            await asyncio.wait_for(broker.entered.wait(), timeout=_DEADLINE)
            second = asyncio.create_task(client.post(f"/orders/{order_id}/cancel", headers=headers))

            # Give the second request a chance to reach the handler (it
            # cannot, once serialized -- which is the point), then let the
            # first finish.
            await asyncio.sleep(0.1)
            broker.release.set()

            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), timeout=_DEADLINE
            )

            raised = [r for r in results if isinstance(r, BaseException)]
            assert not raised, f"a cancel escaped as an exception: {raised!r}"

            codes = sorted(r.status_code for r in results)
            assert 500 not in codes, f"a concurrent cancel 500'd: {codes}"
            assert codes == [200, 409], codes
            losing = next(r for r in results if r.status_code == 409)
            assert "CANCELLED" in losing.text, losing.text
        finally:
            await _cleanup(user_id, instrument.id)


async def test_the_order_is_cancelled_exactly_once_at_the_broker(require_infra):
    """Non-vacuity for the test above, and the harm in its own right: the
    losing request must not reach the broker at all, rather than sending a
    second cancel for an order already cancelled."""
    instrument = await _instrument()
    broker = _GatedBroker()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            order_id = await _resting_order(client, headers, user_id, instrument, broker)

            first = asyncio.create_task(client.post(f"/orders/{order_id}/cancel", headers=headers))
            await asyncio.wait_for(broker.entered.wait(), timeout=_DEADLINE)
            second = asyncio.create_task(client.post(f"/orders/{order_id}/cancel", headers=headers))
            await asyncio.sleep(0.1)
            broker.release.set()
            await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=_DEADLINE)

            assert broker.cancels == 1, (
                f"the broker was asked to cancel {broker.cancels} times for one order"
            )
        finally:
            await _cleanup(user_id, instrument.id)


async def test_a_failing_cancel_still_releases_the_lock(require_infra):
    """The risk a new lock introduces, not the one it removes.

    The handler raises `HTTPException(502)` from inside the critical
    section. If `serialize_user_trading` did not unwind on that path, this
    user's account would be wedged for the life of the process: every later
    order or cancel would block forever. A hang here fails on the deadline
    rather than hanging the suite.
    """
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            order_id = await _resting_order(
                client, headers, user_id, instrument, _CancelAlwaysFailsBroker())

            first = await asyncio.wait_for(
                client.post(f"/orders/{order_id}/cancel", headers=headers), timeout=_DEADLINE)
            assert first.status_code == 502, first.text

            # The lock must be free: a second request has to be answered,
            # not blocked.
            second = await asyncio.wait_for(
                client.post(f"/orders/{order_id}/cancel", headers=headers), timeout=_DEADLINE)
            assert second.status_code == 502, second.text

            # And the order is untouched by either attempt -- round 87's
            # guarantee, which this lock must not disturb.
            listing = await asyncio.wait_for(
                client.get("/orders", headers=headers), timeout=_DEADLINE)
            order = next(o for o in listing.json() if o["id"] == order_id)
            assert order["status"] == "ACKNOWLEDGED", order
        finally:
            await _cleanup(user_id, instrument.id)


async def test_a_sequential_duplicate_cancel_still_returns_409(require_infra):
    """Control: the ordinary non-concurrent path is unchanged. An over-fix
    that swallowed the second cancel into a 200 would pass the headline and
    fail here."""
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            order_id = await _resting_order(client, headers, user_id, instrument, _RestingBroker())

            first = await asyncio.wait_for(
                client.post(f"/orders/{order_id}/cancel", headers=headers), timeout=_DEADLINE)
            assert first.status_code == 200, first.text

            second = await asyncio.wait_for(
                client.post(f"/orders/{order_id}/cancel", headers=headers), timeout=_DEADLINE)
            assert second.status_code == 409, second.text
            assert "CANCELLED" in second.text
        finally:
            await _cleanup(user_id, instrument.id)


def test_every_route_that_mutates_the_order_book_is_serialized():
    """Structural, and labelled as such: it asserts the SET of routes.

    `place_order` and `execute_options_strategy` have taken this lock since
    rounds 152 and 159; `cancel_order` was the third mutator and did not,
    which is this file's bug. Checking the set means a fourth cannot be
    added without one.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "api"
    expected = {"place_order", "cancel_order", "execute_options_strategy"}

    serialized = set()
    for path in (root / "orders.py", root / "options.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # The DEPENDENCY, not merely the imported name -- a leftover
            # `from ... import serialize_user_trading` with the parameter
            # deleted must not satisfy this (round 159 shipped that escape).
            for default in node.args.defaults + node.args.kw_defaults:
                if (
                    isinstance(default, ast.Call)
                    and isinstance(default.func, ast.Name)
                    and default.func.id == "Depends"
                    and default.args
                    and isinstance(default.args[0], ast.Name)
                    and default.args[0].id == "serialize_user_trading"
                ):
                    serialized.add(node.name)

    assert serialized == expected, f"routes taking the per-user trade lock: {serialized}"
