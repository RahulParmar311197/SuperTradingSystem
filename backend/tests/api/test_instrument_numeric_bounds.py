"""`POST /instruments` had no bounds on any of its numeric fields.

`instruments` is not an ordinary request body. The row is global, shared by
every user, writable by any authenticated one, and there is no endpoint
that can edit or delete it (see `create_instrument`'s docstring). So a bad
value here is not a request that fails and is forgotten — it is a
permanent, unrepairable property of that symbol for everyone.

Measured against the live schema before these bounds existed:

    lot_size=0        -> 201
    lot_size=-50      -> 201
    lot_size=2**31    -> 500   (raw asyncpg DataError; the column is INTEGER)
    tick_size=0       -> 201
    tick_size=-1      -> 201
    tick_size=1e30    -> 500   (NumericValueOutOfRange; NUMERIC(18,6))
    strike=0          -> 201
    strike=-25000     -> 201
    strike=1e30/inf   -> 500   (NumericValueOutOfRange; NUMERIC(18,4))

And the accepted ones are the worse half. Driving `POST /options/execute`
against a `lot_size=0` contract:

    execute -> 201
    payoff:    max_profit 0.0, max_loss 0.0, net_premium 0.0
    legs:      [('ACKNOWLEDGED', None)]
    positions: []

A strategy reported as executed that did nothing, and a `RiskEvent` row
recording an approval of a position that had no risk only because it had
no size. `lot_size=-50` is the other direction: `payoff` is
`sign * (intrinsic - premium) * quantity * lot_size`, so a negative lot
size turns a long call into a short one.

Two layers, and they are not the same check. The request bounds decide
what can be *written*; the guard in `POST /options/execute` decides what
may be *read*, which is what protects rows registered before the bounds
existed — and every deployed database has some.

Same family as round 106, which restated the `instruments` string widths
in this schema after measuring four 500s, and stopped at the strings.
"""

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


def _symbol() -> str:
    return f"BOUND{uuid.uuid4().hex[:8].upper()}CE"


def _options_payload(**over) -> dict:
    payload = {
        "symbol": _symbol(),
        "exchange": "NSE",
        "market": "OPTIONS",
        "instrument_type": "OPTION",
        "underlying": "NIFTY",
        "expiry": str(date.today() + timedelta(days=7)),
        "strike": 25000.0,
        "option_type": "CALL",
        "lot_size": 50,
    }
    payload.update(over)
    return payload


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"bounds-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Bounds"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    headers = {"Authorization": f"Bearer {token}"}
    r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
    assert r.status_code == 200, r.text
    return headers, user_id


async def _cleanup(user_id: uuid.UUID, symbols: list[str]) -> None:
    """Child rows first — orders reference instruments, events reference
    orders, and an FK violation in teardown has reddened five earlier
    rounds."""
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        for order_id in order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Trade).where(Trade.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        await db.execute(delete(RiskEvent).where(RiskEvent.user_id == user_id))
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        if symbols:
            await db.execute(delete(Instrument).where(Instrument.symbol.in_(symbols)))
        await db.commit()


# --- layer 1: what may be written ----------------------------------------


REFUSED = [
    # (field, value, what it did before)
    ("lot_size", 0, "201, and every quantity and payoff number became 0.0"),
    ("lot_size", -50, "201, and every payoff number inverted"),
    ("lot_size", 2**31, "500, a raw asyncpg DataError (the column is INTEGER)"),
    ("tick_size", 0.0, "201"),
    ("tick_size", -1.0, "201"),
    ("tick_size", 1e30, "500, NumericValueOutOfRange on NUMERIC(18,6)"),
    ("strike", 0.0, "201"),
    ("strike", -25000.0, "201, a call struck below every spot price there is"),
    ("strike", 1e30, "500, NumericValueOutOfRange on NUMERIC(18,4)"),
    ("strike", float("inf"), "500, the same"),
]


@pytest.mark.parametrize("field,value,before", REFUSED, ids=[f"{f}={v}" for f, v, _ in REFUSED])
async def test_an_out_of_range_instrument_number_is_a_422_naming_the_field(field, value, before, require_infra):
    """Behavioural proof. Each of these was measured, and each was either a
    silent 201 or a 500 with a traceback and no field name in it."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post("/instruments", json=_options_payload(**{field: value}), headers=headers)
            assert r.status_code == 422, f"{field}={value} was {before}; it answered {r.status_code}: {r.text[:200]}"
            assert any(
                field in str(d.get("loc", "")) for d in r.json()["detail"]
            ), f"the 422 must name {field}: {r.text[:300]}"
        finally:
            await _cleanup(user_id, [])


async def test_nothing_is_registered_when_a_number_is_refused(require_infra):
    """Behavioural proof, and the one that makes the 422 mean something. A
    422 that still wrote the row would be worse than the 201 it replaced,
    because the symbol is then taken and cannot be re-registered."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        payload = _options_payload(lot_size=0)
        try:
            assert client.post("/instruments", json=payload, headers=headers).status_code == 422
            async with async_session_factory() as db:
                found = (
                    await db.execute(select(Instrument).where(Instrument.symbol == payload["symbol"]))
                ).scalar_one_or_none()
            assert found is None, "a refused registration must leave no row"
        finally:
            await _cleanup(user_id, [payload["symbol"]])


ACCEPTED = [
    ("lot_size", 1),          # an equity-style contract, and the default
    ("lot_size", 1_000_000),  # the upper bound itself
    ("tick_size", 0.0001),
    ("strike", 0.05),
    ("strike", 1e9),          # the upper bound itself
]


@pytest.mark.parametrize("field,value", ACCEPTED, ids=[f"{f}={v}" for f, v in ACCEPTED])
async def test_a_legitimate_extreme_still_registers(field, value, require_infra):
    """Control against over-tightening. A bound that refuses a real
    instrument would make this endpoint the outage instead of the guard, so
    both ends of each range are exercised, not just the rejected side."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        payload = _options_payload(**{field: value})
        try:
            r = client.post("/instruments", json=payload, headers=headers)
            assert r.status_code == 201, r.text
            assert r.json()[field] == pytest.approx(value)
        finally:
            await _cleanup(user_id, [payload["symbol"]])


async def test_an_ordinary_registration_stores_exactly_what_was_sent(require_infra):
    """Control. The common case must be untouched, and the values must come
    back as sent rather than as defaults — a bound that quietly substituted
    a default would pass a status-code-only test."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        payload = _options_payload(lot_size=75, tick_size=0.05, strike=24800.0)
        try:
            r = client.post("/instruments", json=payload, headers=headers)
            assert r.status_code == 201, r.text
            body = r.json()
            assert (body["lot_size"], body["tick_size"], body["strike"]) == (75, 0.05, 24800.0)
        finally:
            await _cleanup(user_id, [payload["symbol"]])


# --- layer 2: what may be read -------------------------------------------
#
# The request bounds cannot reach an existing row, and this is where the
# harm actually lands, so these drive POST /options/execute against a
# contract inserted straight into the table — exactly the shape a database
# written before this change already holds.


async def _insert_raw_option(symbol: str, lot_size: int) -> None:
    async with async_session_factory() as db:
        db.add(
            Instrument(
                symbol=symbol,
                exchange="NSE",
                market=MarketType.OPTIONS,
                instrument_type="OPTION",
                underlying="NIFTY",
                expiry=date.today() + timedelta(days=7),
                strike=25000.0,
                option_type=OptionType.CALL,
                lot_size=lot_size,
            )
        )
        await db.commit()


@pytest.mark.parametrize("lot_size", [0, -50])
async def test_executing_against_an_unusable_contract_is_refused_not_faked(lot_size, require_infra):
    """Behavioural proof at the read side, where the request validator
    cannot stand in.

    Measured on a `lot_size=0` row before this guard: 201, all three payoff
    numbers 0.0, the leg ACKNOWLEDGED, and no position. The caller was told
    a strategy had executed.
    """
    symbol = _symbol()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        await _insert_raw_option(symbol, lot_size)
        try:
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "long_call",
                    "legs": [{"symbol": symbol, "direction": "LONG", "quantity": 1, "premium": 100.0}],
                },
                headers=headers,
            )
            assert r.status_code == 422, f"answered {r.status_code}: {r.text[:300]}"
            assert "lot_size" in r.json()["detail"], r.text

            async with async_session_factory() as db:
                orders = (await db.execute(select(Order).where(Order.user_id == user_id))).scalars().all()
            assert orders == [], "a refused strategy must not have reached the broker"
        finally:
            await _cleanup(user_id, [symbol])


async def test_a_properly_registered_contract_still_executes(require_infra):
    """Control for the read guard. It must refuse the broken rows and only
    those — a guard that rejected ordinary contracts would close the only
    path that can open *or close* an options position."""
    symbol = _symbol()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        await _insert_raw_option(symbol, 50)
        try:
            r = client.post(
                "/options/execute",
                json={
                    "strategy_name": "long_call",
                    "legs": [{"symbol": symbol, "direction": "LONG", "quantity": 1, "premium": 100.0}],
                },
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            # Derived from this fixture, not copied: 1 lot x 50 per lot x
            # a premium of 100 is a 5,000 debit, and a long call cannot
            # lose more than what it cost.
            assert body["net_premium"] == pytest.approx(5000.0)
            assert body["max_loss"] == pytest.approx(-5000.0)
        finally:
            await _cleanup(user_id, [symbol])
