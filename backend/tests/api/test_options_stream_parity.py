"""Options executions never reached the live order/position streams.

`POST /orders` has incremented `ORDER_COUNT` and published to
`/ws/orders` and `/ws/positions` for as long as those existed.
`POST /options/execute` places real orders through the same
broker/risk/persistence pipeline -- its own docstring says so -- and did
neither. Measured on one open socket, same account:

    POST /orders          -> 201, /ws/orders received 1 event
    POST /options/execute -> 201, /ws/orders received 0 events

A client watching its own live order feed saw equity orders appear and
options executions never arrive at all, and the metric under-counted every
options fill.

This was found by a mechanical diff of what each path calls rather than by
noticing it: five previous rounds (71, 79, 80, 159, 161) each found one
capability on `/orders` missing here, so the sixth was worth looking for by
construction. The same sweep also found that the options path has no
`no_abnormal_price_jump` gate; that one is NOT fixed here, because the gate
measures a jump in the traded symbol and an options contract has no candles
in this system -- wiring it naively would produce exactly the vacuously
passing check round 236 had to undo. Which price it should watch (the
underlying's, via `Instrument.underlying`) is a modelling decision.
"""

import asyncio
import json
import queue
import uuid
from datetime import date, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.core.metrics import ORDER_COUNT
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app

# Long enough for a real publish to land, short enough that a channel that
# stays silent -- which is the bug -- reports that instead of hanging.
# Round 152/156's lesson: every wait here is bounded.
_DEADLINE = 4.0


def _drain(ws) -> list[dict]:
    """Everything that arrived within the deadline. Returns [] rather than
    blocking forever when nothing is published, which is the whole point."""
    got: list[dict] = []
    while True:
        try:
            message = ws._send_queue.get(timeout=_DEADLINE)
        except queue.Empty:
            return got
        if isinstance(message, BaseException):
            return got
        got.append(json.loads(message["text"]))


async def _contract(kind: OptionType, strike: float) -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"SP{uuid.uuid4().hex[:8].upper()}", exchange="NSE", market=MarketType.OPTIONS,
            instrument_type="OPT", option_type=kind, strike=strike, lot_size=50,
            expiry=date.today() + timedelta(days=21), active=True,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _equity() -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"SE{uuid.uuid4().hex[:8].upper()}", exchange="NSE",
            market=MarketType.EQUITY, instrument_type="EQ", active=True,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _user(client) -> tuple[uuid.UUID, dict, str]:
    email = f"sp-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(
            id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"),
            name="Stream Parity", trading_permissions=[TradingPermission.LIVE_TRADE.value],
        )
        db.add(row)
        await db.commit()
        user_id = row.id
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    return user_id, {"Authorization": f"Bearer {token}"}, token


def _spread(long_leg: Instrument, short_leg: Instrument) -> dict:
    return {
        "strategy_name": f"Bull call spread {uuid.uuid4().hex[:6]}",
        "legs": [
            {"symbol": long_leg.symbol, "direction": "LONG", "quantity": 1, "premium": 5.0},
            {"symbol": short_leg.symbol, "direction": "SHORT", "quantity": 1, "premium": 2.0},
        ],
    }


async def _cleanup(user_id: uuid.UUID, instruments: list[Instrument]) -> None:
    from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS

    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        for model in (Notification, RiskEvent, AuditLog, UserSession):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        for row in instruments:
            await db.execute(delete(Instrument).where(Instrument.id == row.id))
        await db.commit()
    for cache in (_STACKS, _STACK_LOCKS, _TRADE_LOCKS):
        cache.pop(user_id, None)


# --- the finding ----------------------------------------------------------


async def test_an_options_execution_reaches_the_order_stream(require_infra):
    """The headline. One event per leg, because each leg is its own real
    order at the broker -- the unit the orders channel already speaks in."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        user_id, headers, token = await _user(client)
        try:
            with client.websocket_connect(f"/ws/orders?token={token}") as ws:
                await asyncio.sleep(0.3)  # a relay subscribes after its handshake returns
                r = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg))
                assert r.status_code == 201, r.text
                events = _drain(ws)
            symbols = sorted(e["symbol"] for e in events)
            assert symbols == sorted([long_leg.symbol, short_leg.symbol]), (
                f"both legs must reach /ws/orders -- got {symbols}"
            )
        finally:
            await _cleanup(user_id, [long_leg, short_leg])


async def test_an_options_execution_reaches_the_position_stream(require_infra):
    """And the position book, once for the batch rather than once per leg."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        user_id, headers, token = await _user(client)
        try:
            with client.websocket_connect(f"/ws/positions?token={token}") as ws:
                await asyncio.sleep(0.3)
                r = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg))
                assert r.status_code == 201, r.text
                events = _drain(ws)
            assert len(events) == 1, f"one snapshot per batch, not per leg -- got {len(events)}"
            streamed = sorted(p["symbol"] for p in events[0]["positions"])
            assert streamed == sorted([long_leg.symbol, short_leg.symbol]), streamed
        finally:
            await _cleanup(user_id, [long_leg, short_leg])


async def test_an_options_execution_counts_toward_the_order_metric(require_infra):
    """`ORDER_COUNT` under-counted every options fill. Read before and
    after rather than asserted absolutely: the counter is process-wide and
    other tests move it."""
    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    with TestClient(app) as client:
        user_id, headers, _token = await _user(client)
        try:
            before = ORDER_COUNT.labels("MONITORING")._value.get()
            r = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg))
            assert r.status_code == 201, r.text
            after = ORDER_COUNT.labels("MONITORING")._value.get()
            assert after - before == 2, f"two legs, two counted orders -- got {after - before}"
        finally:
            await _cleanup(user_id, [long_leg, short_leg])


# --- what the fix must not break -----------------------------------------


async def test_an_equity_order_still_publishes_exactly_once(require_infra):
    """The control. Adding publishes to the options path must not change
    what the single-order path streams."""
    equity = await _equity()
    with TestClient(app) as client:
        user_id, headers, token = await _user(client)
        try:
            with client.websocket_connect(f"/ws/orders?token={token}") as ws:
                await asyncio.sleep(0.3)
                r = client.post(
                    "/orders",
                    headers=headers,
                    json={"symbol": equity.symbol, "direction": "LONG", "order_type": "MARKET",
                          "entry": 104.0, "stop": 103.0},
                )
                assert r.status_code == 201, r.text
                events = _drain(ws)
            assert len(events) == 1, f"one order, one event -- got {len(events)}"
            assert events[0]["symbol"] == equity.symbol
        finally:
            await _cleanup(user_id, [equity])


async def test_a_dead_relay_does_not_fail_the_execution(require_infra):
    """The publish is best-effort on `/orders` -- it swallows everything so
    a websocket relay being down never fails a trade. The options path must
    inherit that, not acquire a new way for a fill to 500."""
    import app.api.orders as orders_module

    long_leg, short_leg = await _contract(OptionType.CALL, 100.0), await _contract(OptionType.CALL, 110.0)
    original = orders_module.publish

    async def _explode(*args, **kwargs):
        raise RuntimeError("relay is down")

    with TestClient(app) as client:
        user_id, headers, _token = await _user(client)
        orders_module.publish = _explode
        try:
            r = client.post("/options/execute", headers=headers, json=_spread(long_leg, short_leg))
            assert r.status_code == 201, f"a dead relay must not fail the strategy: {r.text}"
        finally:
            orders_module.publish = original
            await _cleanup(user_id, [long_leg, short_leg])
