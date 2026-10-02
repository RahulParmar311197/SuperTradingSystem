"""A Redis outage raised an unhandled ConnectionError on two read paths.

Rounds 124 and 125 settled the posture for `POST /auth/login`,
`POST /auth/register` and `GET /health`: a Redis outage gets a legible
refusal, not a traceback -- 125's own note calls `/health` "the one
endpoint you consult during an outage". Two readers of
`app.core.redis.get_latest_price` were left behind.

Measured through the real app with Redis actually stopped:

    GET /quotes                        -> raised ConnectionError (500)
    GET /portfolio, one position open  -> raised ConnectionError (500)
    GET /portfolio, nothing open       -> 200
    POST /orders                       -> 503, already legible
    GET /health                        -> 200, "redis": "DOWN"

The `/portfolio` pair is the point. It broke only when a position was
open, because `_mark_open_positions_to_market`'s loop body never runs
otherwise -- so it failed in exactly the case you would consult it
("what am I holding?") and looked healthy in the case you would not.
That is also why the first probe of this round recorded `/portfolio` as
fine: a false negative from measuring with an empty book.

`POST /orders` was NOT a third site, checked rather than assumed: its
trade lock already turns an unreachable Redis into a 503, so the order
path fails closed and legibly. The hypothesis that it 500'd was wrong.

TWO DIFFERENT RIGHT ANSWERS, which is why this is not one blanket guard:

* `/quotes` returns 503. `None` already means "no tick for this symbol",
  so returning it for "the cache is unreachable" would report absence of
  data as data, and a caller pricing against that would be reading a
  fabricated fact.
* `/portfolio` still answers 200 with the positions unmarked. Balance,
  realized P&L, position count and the positions themselves come from
  Postgres and are still true during a cache outage; refusing the whole
  response would withhold facts it holds. Treating an unreachable cache
  like a missing tick is the rule that path already follows for a symbol
  with no cached price.

The outage is simulated by making `get_latest_price` raise
`redis.exceptions.ConnectionError` -- a real `RedisError` subclass, which
is what the live measurement produced -- rather than by stopping the
server, which inside a test run would break every other test in the
process.
"""

import uuid

from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import delete, select

import app.api.markets as markets_module
import app.api.orders as orders_module
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app


async def _instrument() -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"RO{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
            lot_size=1,
            tick_size=0.05,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first, and by instrument as well as by user."""
    async with async_session_factory() as db:
        for instrument_id in instrument_ids:
            await db.execute(delete(Trade).where(Trade.instrument_id == instrument_id))
            await db.execute(delete(Position).where(Position.instrument_id == instrument_id))
        for user_id in user_ids:
            order_ids = (
                await db.execute(select(Order.id).where(Order.user_id == user_id))
            ).scalars().all()
            for order_id in order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
            for model in (Trade, Position, Order, Notification, AuditLog, RiskEvent, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        for instrument_id in instrument_ids:
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


def _login(client: TestClient, label: str) -> tuple[dict, uuid.UUID]:
    email = f"{label}{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


def _break_redis(monkeypatch, module) -> None:
    async def _raise(symbol: str):
        raise RedisConnectionError("Error 111 connecting to localhost:6379")

    monkeypatch.setattr(module, "get_latest_price", _raise)


# --- /quotes --------------------------------------------------------------


async def test_quotes_refuses_legibly_when_the_cache_is_unreachable(require_infra, monkeypatch):
    """The headline for this path: it used to raise, i.e. a 500."""
    with TestClient(app) as client:
        headers, user_id = _login(client, "roquotes")
        try:
            _break_redis(monkeypatch, markets_module)
            r = client.get("/quotes", params={"symbols": ["INFY", "TCS"]}, headers=headers)
            assert r.status_code == 503, r.text
            assert "cache" in r.text.lower(), r.text
        finally:
            await _cleanup([user_id], [])


async def test_quotes_does_not_report_an_outage_as_a_missing_price(require_infra, monkeypatch):
    """The distinction that makes 503 the right answer rather than `None`:
    an unreachable cache must not be reported in the same shape as a
    symbol nobody has quoted."""
    with TestClient(app) as client:
        headers, user_id = _login(client, "roquotesnull")
        try:
            healthy = client.get("/quotes", params={"symbols": ["INFY"]}, headers=headers)
            assert healthy.status_code == 200, healthy.text
            assert healthy.json() == {"INFY": None}, healthy.text

            _break_redis(monkeypatch, markets_module)
            broken = client.get("/quotes", params={"symbols": ["INFY"]}, headers=headers)
            assert broken.status_code != 200, broken.text
            assert broken.json() != {"INFY": None}, "an outage was reported as 'no price'"
        finally:
            await _cleanup([user_id], [])


async def test_quotes_still_answers_when_the_cache_is_reachable(require_infra):
    """Non-vacuity. A guard that refused every request would pass both
    tests above."""
    with TestClient(app) as client:
        headers, user_id = _login(client, "roquotesok")
        try:
            r = client.get("/quotes", params={"symbols": ["INFY", "TCS"]}, headers=headers)
            assert r.status_code == 200, r.text
            assert set(r.json()) == {"INFY", "TCS"}, r.text
        finally:
            await _cleanup([user_id], [])


# --- /portfolio -----------------------------------------------------------


async def _open_a_position(client: TestClient, headers: dict, symbol: str) -> None:
    assert client.post(
        "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers
    ).status_code == 200
    r = client.post(
        "/orders",
        json={"symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": 100.0, "stop": 95.0},
        headers=headers,
    )
    assert r.status_code == 201, r.text


async def test_portfolio_still_serves_its_stored_facts_during_an_outage(require_infra, monkeypatch):
    """The headline for this path, and the case the first probe missed: it
    raised only with a position open."""
    instrument = await _instrument()
    user_ids: list[uuid.UUID] = []
    try:
        with TestClient(app) as client:
            headers, user_id = _login(client, "roportfolio")
            user_ids.append(user_id)
            await _open_a_position(client, headers, instrument.symbol)

            _break_redis(monkeypatch, orders_module)
            r = client.get("/portfolio", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            # The Postgres-sourced facts survive the cache outage...
            assert body["open_position_count"] == 1, body
            assert body["balance"] == 100000.0, body
            # ...and the mark is simply absent rather than guessed.
            assert body["total_unrealized_pnl"] == 0.0, body
    finally:
        await _cleanup(user_ids, [instrument.id])


async def test_portfolio_with_nothing_open_was_never_the_broken_case(require_infra, monkeypatch):
    """Pins why this round's first measurement was a false negative: with
    an empty book the loop body never runs, so the endpoint answered 200
    both before and after the fix. A test written only this way would
    have proved nothing."""
    with TestClient(app) as client:
        headers, user_id = _login(client, "roportfolioempty")
        try:
            _break_redis(monkeypatch, orders_module)
            r = client.get("/portfolio", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["open_position_count"] == 0, r.text
        finally:
            await _cleanup([user_id], [])


async def test_portfolio_marks_to_market_when_the_cache_is_reachable(require_infra):
    """Non-vacuity for the tolerant branch: with Redis healthy and a
    cached price, the position must still be marked. A fix that simply
    stopped marking would pass every test above."""
    from app.core.redis import set_latest_price

    instrument = await _instrument()
    user_ids: list[uuid.UUID] = []
    try:
        with TestClient(app) as client:
            headers, user_id = _login(client, "roportfoliomark")
            user_ids.append(user_id)
            await _open_a_position(client, headers, instrument.symbol)
            await set_latest_price(instrument.symbol, 120.0)

            r = client.get("/portfolio", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["total_unrealized_pnl"] > 0.0, body
            assert body["equity"] > body["balance"], body
    finally:
        await _cleanup(user_ids, [instrument.id])
