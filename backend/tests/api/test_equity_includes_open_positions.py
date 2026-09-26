"""Reported equity ignored every open position.

`AccountInfo.equity` is `balance + open unrealized` -- that is what a
real adapter means by it (see `UpstoxBroker.get_account`) and what
round 170 made `MockBroker` derive. Three readers report it:
`GET /portfolio`, `GET /paper/{id}`, and every `portfolio_snapshots`
row. Two of the three were wrong, for two different reasons.

Measured, a 100-share long opened at 100 with the market at 120:

    GET /portfolio -> balance              100000.0
                      equity               100000.0   <- as if nothing open
                      total_unrealized_pnl   2000.0   <- same payload

    snapshot       -> balance 100000.0  equity 100000.0
                      (position_manager unrealized 0.0 -- nothing marked
                       at all on that path)

Three causes, and the first alone fixes neither endpoint:

1. `_mark_open_positions_to_market` marked `PositionManager` and not the
   broker, so `BrokerPosition.unrealized_pnl` stayed 0.0 and the derived
   equity was just cash. `PaperTradingEngine.on_candle` already marks
   both, two lines apart; the manual path marked one.
2. `GET /portfolio` read `get_account()` on the line BEFORE the mark, so
   with (1) fixed it still reported the pre-mark figure.
3. `portfolio_snapshots._snapshot_one` never called that helper at all,
   and read the account first as well -- and `snapshot_all_stacks` only
   writes rows for stacks that HAVE open positions, so every row it
   wrote was for exactly the case it got wrong. There is no
   `unrealized_pnl` column on that table: `equity` is the only place
   open P&L can appear in the stored history.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS
from app.auth.security import TokenType, decode_token
from app.core.redis import get_redis, set_latest_price
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, PortfolioSnapshot, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.trading.portfolio_snapshots import snapshot_all_stacks

BALANCE = 100_000.0


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"eq-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post(
        "/auth/register", json={"email": email, "password": "testpass123", "name": "Eq"}
    ).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(
        "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers
    ).status_code == 200
    return headers, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _instrument() -> tuple[uuid.UUID, str]:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"EQ{uuid.uuid4().hex[:7].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id, row.symbol


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID, symbol: str) -> None:
    """Child rows first, then the process-wide caches this path uses."""
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        for order_id in order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
        for model in (Order, Trade, Position, PortfolioSnapshot, Notification, RiskEvent, AuditLog, UserSession):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()
    _STACKS.pop(user_id, None)
    _TRADE_LOCKS.pop(user_id, None)
    _STACK_LOCKS.pop(user_id, None)
    # The probe writes a real Redis key; leaving it behind would leak into
    # any later test that reused the symbol.
    try:
        await (await get_redis()).delete(f"price:{symbol}")
    except Exception:  # pragma: no cover - cleanup must not mask a failure
        pass


async def _open_long(client: TestClient, headers: dict, symbol: str) -> float:
    r = client.post(
        "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()["quantity"]


# --- GET /portfolio -------------------------------------------------------


@pytest.mark.parametrize(
    "price, expected_unrealized",
    [
        (120.0, 2_000.0),   # in profit
        (90.0, -1_000.0),   # underwater: a fix that only ever added would pass the first
    ],
)
async def test_portfolio_equity_agrees_with_its_own_unrealized_pnl(require_infra, price, expected_unrealized):
    """The headline. One payload cannot say "you are up 2,000" and
    "your equity is exactly your cash"."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            assert await _open_long(client, headers, symbol) == 100.0
            await set_latest_price(symbol, price)

            body = client.get("/portfolio", headers=headers).json()
            assert body["total_unrealized_pnl"] == pytest.approx(expected_unrealized)
            assert body["balance"] == pytest.approx(BALANCE), "nothing realized yet"
            assert body["equity"] == pytest.approx(body["balance"] + body["total_unrealized_pnl"])
        finally:
            await _cleanup(user_id, instrument_id, symbol)


async def test_a_flat_account_reports_equity_equal_to_cash(require_infra):
    """Boundary control: with nothing open the two must coincide, so the
    assertion above is about the position and not about equity having
    been given some constant offset."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            body = client.get("/portfolio", headers=headers).json()
            assert body["open_position_count"] == 0
            assert body["total_unrealized_pnl"] == pytest.approx(0.0)
            assert body["equity"] == pytest.approx(body["balance"])
        finally:
            await _cleanup(user_id, instrument_id, symbol)


async def test_a_symbol_with_no_cached_price_is_left_alone_not_guessed(require_infra):
    """The helper's documented rule, unchanged by marking a second book:
    no cached tick means no mark, rather than marking at a made-up
    price. Both figures stay at their unmarked values and still agree."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _open_long(client, headers, symbol)
            # Deliberately no set_latest_price for this symbol.
            body = client.get("/portfolio", headers=headers).json()
            assert body["open_position_count"] == 1
            assert body["total_unrealized_pnl"] == pytest.approx(0.0)
            assert body["equity"] == pytest.approx(body["balance"])
        finally:
            await _cleanup(user_id, instrument_id, symbol)


async def test_marking_to_market_does_not_fire_the_resting_protective_stop(require_infra):
    """The over-fix control, and the reason the mark passes
    `is_market_print=False`.

    Round 169: letting a price act as the broker's tape gives resting
    stops a chance to fire behind the app's back, leaving the broker
    short a position `PositionManager` reports as open. A cached price
    BELOW the protective stop is the exact trigger, and reading
    `GET /portfolio` must never execute a trade.
    """
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _open_long(client, headers, symbol)  # stop at 95
            stack = _STACKS[user_id]
            resting_before = len(stack.broker._resting)
            assert resting_before == 1, "the protective stop must be resting for this to prove anything"

            await set_latest_price(symbol, 90.0)  # straight through the stop
            client.get("/portfolio", headers=headers)

            assert len(stack.broker._resting) == resting_before, "reading the portfolio fired a stop"
            broker_position = stack.broker._positions.get(symbol)
            assert broker_position is not None and broker_position.quantity == pytest.approx(100.0), (
                "the broker's book moved while nobody placed an order"
            )
        finally:
            await _cleanup(user_id, instrument_id, symbol)


# --- portfolio_snapshots --------------------------------------------------


async def test_the_persisted_snapshot_carries_the_open_positions_pnl(require_infra):
    """The second layer, standing alone: nothing else touches the stack
    before the snapshot runs, so this fails unless the snapshot path does
    its own marking. `equity` is the only column that can carry open P&L.
    """
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _open_long(client, headers, symbol)
            await set_latest_price(symbol, 120.0)

            assert await snapshot_all_stacks() >= 1
            async with async_session_factory() as db:
                rows = (
                    await db.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id))
                ).scalars().all()
            assert len(rows) == 1
            assert float(rows[0].balance) == pytest.approx(BALANCE)
            assert float(rows[0].equity) == pytest.approx(BALANCE + 2_000.0)
        finally:
            await _cleanup(user_id, instrument_id, symbol)


async def test_a_snapshot_with_no_cached_price_records_cash_as_equity(require_infra):
    """Non-vacuity control for the row above: the same code path with
    nothing to mark against records the two as equal, so that assertion
    is about the 2,000 and not about equity being inflated generally."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _open_long(client, headers, symbol)
            # No cached price for this symbol.
            assert await snapshot_all_stacks() >= 1
            async with async_session_factory() as db:
                rows = (
                    await db.execute(select(PortfolioSnapshot).where(PortfolioSnapshot.user_id == user_id))
                ).scalars().all()
            assert len(rows) == 1
            assert float(rows[0].equity) == pytest.approx(float(rows[0].balance))
        finally:
            await _cleanup(user_id, instrument_id, symbol)
