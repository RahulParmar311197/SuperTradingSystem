"""`POST /instruments` must be able to register a tradable options contract.

`instruments` is the only table an options contract can live in and
`POST /instruments` is the only route that creates one -- there is no PUT,
PATCH or DELETE on instruments, and `Instrument(**payload.model_dump())` in
`app/api/markets.py` is the only `Instrument(...)` construction anywhere in
`app/`.

`InstrumentCreateRequest` carried none of `underlying`, `expiry`, `strike`,
`option_type`, though all four are real columns. Pydantic's default
`extra="ignore"` therefore dropped them from a caller's body, the endpoint
returned 201, and `InstrumentResponse` did not expose them either, so the
loss was unobservable. The row was permanently unusable: every options
reader requires `option_type` and `strike`, and nothing can repair it.

Why the existing tests missed it. `tests/api/test_instruments.py` is the
file that looks like coverage of this endpoint, but its shared `_payload()`
helper hardcodes `market="EQUITY", instrument_type="EQ"` and takes only a
symbol, so no test ever sent `market="OPTIONS"` here (shape b).
`tests/api/test_options_execute.py` is the file that looks like coverage of
options execution, but `_make_two_leg_instruments` builds `Instrument(...)`
ORM rows directly through `async_session_factory()` with `strike` and
`option_type` already filled in and commits them -- it never calls the
endpoint. So the suite proved the execute pipeline works on rows the API
itself could not produce (shapes b and e), and `market="OPTIONS"` was a
user-selectable enum value with no test touching it on this route (shape f).
"""

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, OptionType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.trading import Order, OrderEvent, Position, Trade
from app.database.models.users import BrokerAccount, User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio

EXPIRY = date(2026, 9, 25)


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


async def _cleanup(user_id: uuid.UUID, symbols: list[str]) -> None:
    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        for model in (Trade, Order, Position, RiskEvent, Notification, AuditLog, UserSession, BrokerAccount):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        if symbols:
            await db.execute(delete(Instrument).where(Instrument.symbol.in_(symbols)))
        await db.commit()


def _option_payload(symbol: str, **overrides) -> dict:
    payload = {
        "symbol": symbol,
        "exchange": "NSE",
        "market": "OPTIONS",
        "instrument_type": "OPTION",
        "lot_size": 50,
        "underlying": "NIFTY",
        "expiry": EXPIRY.isoformat(),
        "strike": 25000.0,
        "option_type": "CALL",
    }
    payload.update(overrides)
    return payload


async def test_a_registered_option_is_actually_tradable(require_infra):
    # Regression test: the four derivative fields were dropped, so this
    # returned 201 for a row `POST /options/execute` then refused as "not an
    # options contract" -- with no endpoint able to repair it. Multi-leg
    # options execution was unreachable for every instrument the API could
    # create.
    symbol = f"NIFTY{uuid.uuid4().hex[:5].upper()}CE"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "instopt")
        try:
            created = client.post("/instruments", json=_option_payload(symbol), headers=headers)
            assert created.status_code == 201, created.text

            executed = client.post(
                "/options/execute",
                json={
                    "strategy_name": "long_call",
                    "legs": [{"symbol": symbol, "direction": "LONG", "quantity": 1, "premium": 120.0}],
                },
                headers=headers,
            )
            assert executed.status_code == 201, executed.text
        finally:
            await _cleanup(user_id, [symbol])


async def test_the_derivative_fields_are_persisted_and_returned(require_infra):
    # The values must survive the round trip, not merely be accepted. The
    # response carried none of them, which is why the silent drop could not
    # be noticed from the outside.
    symbol = f"NIFTY{uuid.uuid4().hex[:5].upper()}PE"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "instfields")
        try:
            body = client.post(
                "/instruments",
                json=_option_payload(symbol, option_type="PUT", strike=24500.0),
                headers=headers,
            ).json()

            assert body["underlying"] == "NIFTY"
            assert body["expiry"] == EXPIRY.isoformat()
            assert body["strike"] == pytest.approx(24500.0)
            assert body["option_type"] == "PUT"

            async with async_session_factory() as db:
                row = (await db.execute(select(Instrument).where(Instrument.symbol == symbol))).scalar_one()
            assert row.underlying == "NIFTY"
            assert row.expiry == EXPIRY
            assert float(row.strike) == pytest.approx(24500.0)
            assert row.option_type is OptionType.PUT
        finally:
            await _cleanup(user_id, [symbol])


@pytest.mark.parametrize("missing", ["strike", "option_type"])
async def test_an_options_row_without_the_fields_its_readers_need_is_refused(require_infra, missing):
    # The invariant `app/api/options.py` enforces on read, now enforced on
    # write. Accepting the row and failing later is what made this
    # unrecoverable.
    symbol = f"NIFTY{uuid.uuid4().hex[:5].upper()}XX"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "instbad")
        try:
            r = client.post("/instruments", json=_option_payload(symbol, **{missing: None}), headers=headers)
            assert r.status_code == 422, r.text
            assert missing in r.text

            async with async_session_factory() as db:
                assert (
                    await db.execute(select(Instrument).where(Instrument.symbol == symbol))
                ).scalar_one_or_none() is None, "nothing may be written when the payload is refused"
        finally:
            await _cleanup(user_id, [symbol])


async def test_a_non_options_instrument_may_not_carry_a_strike(require_infra):
    # The mirror invariant: an EQUITY row with a strike would read as an
    # option to anything that checks `option_type is not None` later.
    symbol = f"EQ{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "insteq")
        try:
            r = client.post(
                "/instruments",
                json={
                    "symbol": symbol, "exchange": "NSE", "market": "EQUITY",
                    "instrument_type": "EQ", "strike": 100.0,
                },
                headers=headers,
            )
            assert r.status_code == 422, r.text
        finally:
            await _cleanup(user_id, [symbol])


async def test_plain_equity_registration_is_unchanged(require_infra):
    # The control: the common case must not have become harder, and the new
    # optional fields must read back as null rather than absent.
    symbol = f"EQ{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "instplain")
        try:
            r = client.post(
                "/instruments",
                json={"symbol": symbol, "exchange": "NSE", "market": "EQUITY", "instrument_type": "EQ"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
            body = r.json()
            assert body["symbol"] == symbol
            assert body["strike"] is None
            assert body["option_type"] is None
            assert body["expiry"] is None
        finally:
            await _cleanup(user_id, [symbol])


async def test_a_futures_contract_keeps_its_underlying_and_expiry(require_infra):
    # FUTURES has an underlying and an expiry but no strike or option type;
    # those two fields must still round-trip for it.
    symbol = f"FUT{uuid.uuid4().hex[:5].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "instfut")
        try:
            body = client.post(
                "/instruments",
                json={
                    "symbol": symbol, "exchange": "NSE", "market": "FUTURES",
                    "instrument_type": "FUT", "underlying": "NIFTY",
                    "expiry": (EXPIRY + timedelta(days=28)).isoformat(), "lot_size": 50,
                },
                headers=headers,
            ).json()

            assert body["underlying"] == "NIFTY"
            assert body["expiry"] == (EXPIRY + timedelta(days=28)).isoformat()
            assert body["strike"] is None
        finally:
            await _cleanup(user_id, [symbol])
