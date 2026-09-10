"""Tests for the global instrument master (`POST`/`GET /instruments`).

`instruments` is un-owned and shared by every user, and every reader
resolves a symbol with `.scalar_one_or_none()`:
`app/api/orders.py::_get_instrument_by_symbol`, `app/api/options.py`'s
per-leg lookup, `app/risk/portfolio.py::compute_correlated_exposure` and
`app/trading/portfolio_snapshots.py`. "Exactly one row per symbol" was
already the operative contract everywhere -- it was simply never enforced,
by the endpoint or by the schema, and there was no test file here at all.

Every other test in the suite inserts its instrument row directly with a
uuid-suffixed symbol, so the suite was uniquely-symbolled by construction
and structurally could not reach the duplicate case.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.database.models.instruments import Instrument, MarketType
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.notifications import Notification
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register(client: TestClient, label: str, live_trade: bool = False) -> tuple[dict, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    if live_trade:
        r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
        assert r.status_code == 200, r.text
    return headers, user_id


async def _cleanup(user_ids: list[uuid.UUID], symbol: str) -> None:
    async with async_session_factory() as db:
        for user_id in user_ids:
            order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
            if order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
            for model in (Trade, Order, Position, RiskEvent, Notification, AuditLog, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.symbol == symbol))
        await db.commit()


def _payload(symbol: str) -> dict:
    return {"symbol": symbol, "exchange": "NSE", "market": "EQUITY", "instrument_type": "EQ"}


async def test_registering_a_symbol_twice_is_refused(require_infra):
    symbol = f"DUPA{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "instdup")
        try:
            first = client.post("/instruments", json=_payload(symbol), headers=headers)
            assert first.status_code == 201, first.text

            second = client.post("/instruments", json=_payload(symbol), headers=headers)
            assert second.status_code == 409, second.text
            assert symbol in second.json()["detail"]

            # Exactly one row survives, which is what every reader assumes.
            listed = client.get(f"/instruments?symbol={symbol}", headers=headers)
            assert listed.status_code == 200, listed.text
            assert len(listed.json()) == 1
        finally:
            await _cleanup([user_id], symbol)


async def test_a_duplicate_symbol_cannot_lock_a_holder_out_of_closing_their_position(require_infra):
    # This is the harm the 409 above prevents, and it is why this is not
    # merely a tidiness fix.
    #
    # `instruments` is global and `POST /instruments` needs no admin role,
    # so any user -- or an operator re-running a bootstrap script -- could
    # add a second row for a symbol somebody else was holding. Every
    # reader then raised `MultipleResultsFound` -> 500. Worst of all,
    # `_get_instrument_by_symbol` is the *first* statement of
    # `place_order`, so the failure landed before `is_reducing` was
    # computed and the exit-order exemption -- which exists precisely so a
    # position can always be closed -- never ran. Pre-fix, the closing
    # order below returned 500 and the position stayed open with no way
    # out through the API: POST /orders is the only path that closes one,
    # and there is no endpoint to delete or deactivate an instrument.
    symbol = f"DUPB{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        other_headers, other_id = await _register(client, "instother")
        holder_headers, holder_id = await _register(client, "instholder", live_trade=True)
        try:
            assert client.post("/instruments", json=_payload(symbol), headers=other_headers).status_code == 201

            opened = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=holder_headers,
            )
            assert opened.status_code == 201, opened.text
            assert opened.json()["quantity"] == pytest.approx(500 / 5)

            # Somebody else re-registers the same symbol.
            assert client.post("/instruments", json=_payload(symbol), headers=other_headers).status_code == 409

            # The holder can still get out.
            closed = client.post(
                "/orders",
                json={"symbol": symbol, "direction": "SHORT", "entry": 110.0, "stop": 115.0},
                headers=holder_headers,
            )
            assert closed.status_code == 201, closed.text
            assert client.get("/positions", headers=holder_headers).json() == []
        finally:
            await _cleanup([other_id, holder_id], symbol)


async def test_the_database_itself_refuses_a_duplicate_symbol(require_infra):
    # The endpoint check above is read-then-write and so cannot be the
    # guarantee on its own -- two concurrent registrations can both pass
    # it. The unique index on `instruments.symbol` is what actually holds
    # the invariant; this pins that it exists, independent of the API.
    symbol = f"DUPC{uuid.uuid4().hex[:6].upper()}"
    try:
        async with async_session_factory() as db:
            db.add(Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"))
            await db.commit()

        with pytest.raises(IntegrityError):
            async with async_session_factory() as db:
                db.add(Instrument(symbol=symbol, exchange="BSE", market=MarketType.EQUITY, instrument_type="EQ"))
                await db.commit()
    finally:
        async with async_session_factory() as db:
            await db.execute(delete(Instrument).where(Instrument.symbol == symbol))
            await db.commit()


async def test_distinct_symbols_still_register_normally(require_infra):
    # The control: uniqueness must not have made registration itself
    # harder.
    first_symbol = f"OKA{uuid.uuid4().hex[:6].upper()}"
    second_symbol = f"OKB{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "instok")
        try:
            for symbol in (first_symbol, second_symbol):
                r = client.post("/instruments", json=_payload(symbol), headers=headers)
                assert r.status_code == 201, r.text
                assert r.json()["symbol"] == symbol
        finally:
            await _cleanup([user_id], first_symbol)
            await _cleanup([], second_symbol)
