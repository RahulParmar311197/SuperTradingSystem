"""Closing a long below its stop sold it twice and left the account short.

`POST /orders` seeds `MockBroker` with the client's `payload.entry` so a
simulated order has something to fill against. That seed also acted as
the broker's tape, so it gave the resting protective stop a chance to
fire -- at a price the market never printed, taken from the caller's own
request. The stop sold the whole position, and the closing order then
went out on top of it.

Measured through the real ASGI app, a 100-unit long closed at 75 against
a stop of 95:

    after open   broker +100 @ 100   resting 1   app +100
    after close  broker -100 @  75   resting 0   app    0

The account is left SHORT 100 units nobody asked for, with no stop of its
own, while `PositionManager` reports flat -- so no endpoint, notification
or journal row would ever mention it, and only `ReconciliationWorker`
could notice. It is not a rare race either: it fires whenever a long is
closed below its own stop, which is the ordinary shape of a losing exit.

The comment on that very call site already forbids this -- "Never let a
real order's fill price be dictated by the caller" -- and the seed was
doing exactly that, one layer down.

`set_quote(..., is_market_print=False)` records the price without running
the tape. `ensure_protective_stop` still withdraws the stop after the
fill, which is where withdrawing it belongs.

SCOPE. A real broker is unaffected either way: nothing seeds its price
and it fires its own resting orders off its own tape. There is a
separate, narrower window on that path -- the protective stop is
cancelled only after the closing fill, so a stop triggering in between
would double-sell the same way -- which is reasoned rather than measured
and deliberately NOT addressed here.
"""

import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS
from app.auth.security import hash_password
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app

# 0.5% of 100,000 over a 5-wide stop.
OPENED_QUANTITY = 100.0
ENTRY = 100.0
STOP = 95.0
# Below the stop, so the resting SL_M is crossed the instant the closing
# order's quote is seeded. That is what makes this deterministic rather
# than a race.
EXIT = 75.0
EXIT_STOP = 80.0


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _instrument() -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(symbol=f"SQP{uuid.uuid4().hex[:6].upper()}", exchange="NSE",
                         market=MarketType.EQUITY, instrument_type="EQ")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _user(client) -> tuple[uuid.UUID, dict]:
    email = f"sqp-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"),
                   name="Seeded Quote", trading_permissions=[TradingPermission.LIVE_TRADE.value])
        db.add(row)
        await db.commit()
        user_id = row.id
    r = await client.post("/auth/login", json={"email": email, "password": "testpass123"})
    return user_id, {"Authorization": f"Bearer {r.json()['access_token']}"}


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
    for cache in (_STACKS, _STACK_LOCKS, _TRADE_LOCKS):
        cache.pop(user_id, None)


async def _open_a_long(client, headers, symbol: str) -> None:
    r = await client.post("/orders", headers=headers, json={
        "symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": ENTRY, "stop": STOP})
    assert r.status_code == 201, r.text
    assert r.json()["quantity"] == pytest.approx(OPENED_QUANTITY)


async def _close_it(client, headers, symbol: str):
    return await client.post("/orders", headers=headers, json={
        "symbol": symbol, "direction": "SHORT", "order_type": "MARKET",
        "entry": EXIT, "stop": EXIT_STOP})


def _broker_quantity(stack, symbol: str) -> float:
    """What the BROKER thinks is held. `PositionManager` is the app's own
    book and reported flat throughout the bug, so asking it proves
    nothing -- this is the side that was wrong."""
    return sum(p.quantity for p in stack.broker._positions.values() if p.symbol == symbol)


# --- the finding ----------------------------------------------------------


async def test_closing_a_long_below_its_stop_does_not_leave_the_account_short(require_infra):
    """The headline, through the real endpoints."""
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            await _open_a_long(client, headers, instrument.symbol)
            stack = _STACKS[user_id]
            assert _broker_quantity(stack, instrument.symbol) == pytest.approx(OPENED_QUANTITY)

            r = await _close_it(client, headers, instrument.symbol)
            assert r.status_code == 201, r.text

            held = _broker_quantity(stack, instrument.symbol)
            assert held == pytest.approx(0.0), (
                f"the broker is left holding {held} units after a round trip that flattens the "
                f"position -- a naked reversal nobody asked for"
            )
        finally:
            await _cleanup(user_id, instrument.id)


async def test_the_position_is_sold_once_not_twice(require_infra):
    """Non-vacuity for the headline, and the harm stated directly: the
    protective stop must not fill alongside the closing order. Summing
    the broker's fills catches an over-fix that merely nets to zero by
    selling twice and buying once."""
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            await _open_a_long(client, headers, instrument.symbol)
            stack = _STACKS[user_id]

            before = len(stack.broker._orders)
            r = await _close_it(client, headers, instrument.symbol)
            assert r.status_code == 201, r.text

            filled_sells = [
                o for o in stack.broker._orders.values()
                if o.symbol == instrument.symbol
                and o.direction.value == "SHORT"
                and o.filled_quantity
            ]
            assert len(filled_sells) == 1, (
                f"{len(filled_sells)} sell orders filled for one closing request: "
                f"{[(o.order_type.value, o.filled_quantity, o.average_fill_price) for o in filled_sells]}"
            )
            assert filled_sells[0].filled_quantity == pytest.approx(OPENED_QUANTITY)
            assert before  # the opening order and its protective stop exist
        finally:
            await _cleanup(user_id, instrument.id)


async def test_the_protective_stop_is_still_withdrawn_on_the_close(require_infra):
    """The other half of the invariant, and the guard against the lazy
    fix. Never letting the seed fire the tape must not turn into leaving
    a live stop resting against a flat position -- at a real broker that
    becomes a naked reversal the moment price reaches it. Withdrawal is
    `ensure_protective_stop`'s job and must still happen."""
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            await _open_a_long(client, headers, instrument.symbol)
            stack = _STACKS[user_id]
            assert len(stack.broker._resting) == 1, "the fixture must leave a protective stop resting"

            r = await _close_it(client, headers, instrument.symbol)
            assert r.status_code == 201, r.text

            assert stack.broker._resting == {}, (
                "a stop is still resting against a position that no longer exists"
            )
            position = stack.position_manager.get(str(user_id), instrument.symbol)
            assert position is not None and not position.is_open
            assert position.protective_order_id is None
        finally:
            await _cleanup(user_id, instrument.id)


# --- controls -------------------------------------------------------------


async def test_the_realized_pnl_is_unchanged(require_infra):
    """Control: the fix must move the BROKER's book and nothing else. The
    app's own accounting was already right -- it reported flat with
    -2,500 realized -- so an over-fix that changed the fill price or the
    quantity would show up here."""
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            await _open_a_long(client, headers, instrument.symbol)
            r = await _close_it(client, headers, instrument.symbol)
            assert r.status_code == 201, r.text

            stack = _STACKS[user_id]
            position = stack.position_manager.get(str(user_id), instrument.symbol)
            expected = (EXIT - ENTRY) * OPENED_QUANTITY  # -2,500
            assert position.realized_pnl == pytest.approx(expected)
            assert stack.daily_pnl == pytest.approx(expected)

            async with async_session_factory() as db:
                trades = (await db.execute(
                    select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
            assert len(trades) == 1
            assert float(trades[0].pnl) == pytest.approx(expected)
            assert float(trades[0].quantity) == pytest.approx(OPENED_QUANTITY)
        finally:
            await _cleanup(user_id, instrument.id)


async def test_a_stop_still_fires_on_a_real_price_print(require_infra):
    """The control that makes the fix meaningful rather than a way of
    switching protective stops off. `set_quote` with its default still
    runs the tape, which is how a paper stop-loss fills at all -- the
    paper engine feeds every candle through it. An over-fix that stopped
    resting orders firing anywhere passes every test above and fails
    here."""
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            await _open_a_long(client, headers, instrument.symbol)
            stack = _STACKS[user_id]
            assert len(stack.broker._resting) == 1

            # A genuine print, the way app/paper/engine.py feeds candles.
            stack.broker.set_quote(instrument.symbol, ltp=STOP - 1.0)

            assert stack.broker._resting == {}, "a real print must still trigger the resting stop"
            assert _broker_quantity(stack, instrument.symbol) == pytest.approx(0.0), (
                "the stop must actually have closed the position at the broker"
            )
        finally:
            await _cleanup(user_id, instrument.id)


async def test_an_ordinary_profitable_close_is_unaffected(require_infra):
    """Control: the common path, where the exit price never reaches the
    stop, behaved correctly before and must still. Without this the suite
    would only ever exercise the broken shape."""
    instrument = await _instrument()
    async with _client() as client:
        user_id, headers = await _user(client)
        try:
            await _open_a_long(client, headers, instrument.symbol)
            stack = _STACKS[user_id]

            r = await client.post("/orders", headers=headers, json={
                "symbol": instrument.symbol, "direction": "SHORT", "order_type": "MARKET",
                "entry": 110.0, "stop": 115.0})
            assert r.status_code == 201, r.text

            assert _broker_quantity(stack, instrument.symbol) == pytest.approx(0.0)
            assert stack.broker._resting == {}
            position = stack.position_manager.get(str(user_id), instrument.symbol)
            assert position.realized_pnl > 0
        finally:
            await _cleanup(user_id, instrument.id)
