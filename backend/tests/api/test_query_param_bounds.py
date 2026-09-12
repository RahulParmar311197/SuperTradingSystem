"""A client-supplied `limit` must never reach Postgres unvalidated.

`limit` was declared `int` with an upper cap on some endpoints and no
bound at all on others, but with no lower bound anywhere. A negative value
was passed straight into `.limit(...)`, which Postgres rejects
(`InvalidRowCountInLimitClauseError`) -- so `?limit=-1` answered **500**,
a server error for what is purely a bad request. Two endpoints also had no
upper bound, letting one request ask for an entire table.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"bounds-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Bounds"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# (path, needs_extra_query) for every listing endpoint taking a `limit`.
_ENDPOINTS = [
    "/notifications",
    "/ai/chat/history",
]


@pytest.mark.parametrize("path", _ENDPOINTS)
async def test_a_negative_limit_is_rejected_not_a_server_error(require_infra, path):
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(f"{path}?limit=-1", headers=headers)
            # The point of the test: 422, not 500. A 500 here means the
            # value reached the database.
            assert r.status_code == 422, r.text
            assert r.json()["detail"][0]["loc"] == ["query", "limit"]
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize("path", _ENDPOINTS)
async def test_an_oversized_limit_is_rejected(require_infra, path):
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(f"{path}?limit=1000000000", headers=headers)
            assert r.status_code == 422, r.text
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize("path", _ENDPOINTS)
async def test_a_limit_inside_the_bounds_still_works(require_infra, path):
    """Guard against the bounds being set so tightly that ordinary
    requests break."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            for limit in (1, 50):
                r = client.get(f"{path}?limit={limit}", headers=headers)
                assert r.status_code == 200, r.text
                assert r.json() == []
        finally:
            await _cleanup(user_id)


async def test_setups_limit_is_bounded_too(require_infra):
    # `GET /markets/setups` had no upper bound at all. It also needs real
    # query params, so it is checked separately: a rejected `limit` must
    # come back 422 before the handler runs.
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(
                f"/setups?instrument_id={uuid.uuid4()}&timeframe=15m&limit=-1", headers=headers
            )
            assert r.status_code == 422, r.text
            assert any(d["loc"] == ["query", "limit"] for d in r.json()["detail"])
        finally:
            await _cleanup(user_id)


# --- a validation failure must not become a server error -------------------


async def test_a_non_finite_body_value_is_a_422_not_a_500(require_infra):
    """JSON has no literal for infinity, but `1e400` parses to it.

    FastAPI's default handler echoes the offending input in the 422 body,
    so serialising it raised `ValueError: Out of range float values are
    not JSON compliant` -- which the unhandled-exception handler then
    turned into a 500. Every endpoint with a bounded numeric body field
    had this, so it is fixed once in `app/main.py` rather than per-field.

    This uses `/auto-trading/enable` deliberately: its
    `risk_per_trade_pct` bound (`le=100`) predates this change, so a pass
    here is the general handler working, not a field-level fix.
    """
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        client.post("/trading-permissions/grant", json={"permission": "AUTO_TRADE", "confirm": True}, headers=headers)
        try:
            r = client.post("/auto-trading/enable", json={"risk_per_trade_pct": 1e400, "confirm": True}, headers=headers)

            assert r.status_code == 422, r.text
            # The offending value still has to be reported, just printably.
            body = r.text
            assert "risk_per_trade_pct" in body
            assert "inf" in body
        finally:
            await _cleanup(user_id)


# --- an analysis knob is a cost knob too -----------------------------------
#
# The bounds above all landed on a parameter named `limit`. `swing_length`
# is the same class of mistake one name over: a client-supplied integer
# reaching an engine that has its own opinion about what is valid, with
# nothing in between. `detect_swings` *raises* below 1 and grows a
# `2 * swing_length + 1` window per bar above it.


async def _seed_instrument_with_candles(bars: int = 200) -> uuid.UUID:
    """A real instrument with real candles, so `GET /charts/{id}/smc`
    actually reaches `SMCEngine.analyze` rather than short-circuiting on
    an empty series."""
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"SWING{uuid.uuid4().hex[:8].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
            lot_size=1,
            tick_size=0.05,
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)

        base = datetime(2025, 1, 1, 9, 15, tzinfo=timezone.utc)
        db.add_all(
            [
                CandleRow(
                    instrument_id=instrument.id,
                    timeframe="5m",
                    timestamp=base + timedelta(minutes=5 * i),
                    open=100 + (i % 17) - (i % 5),
                    high=102 + (i % 17) - (i % 5),
                    low=98 + (i % 17) - (i % 5),
                    close=101 + (i % 17) - (i % 5),
                    volume=1000.0 + i,
                )
                for i in range(bars)
            ]
        )
        await db.commit()
        return instrument.id


async def _drop_instrument(instrument_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


@pytest.mark.parametrize("swing_length", [0, -1, -100])
async def test_a_swing_length_below_one_is_a_422_not_a_server_error(require_infra, swing_length):
    """Behavioural proof. `detect_swings` raises
    `ValueError("swing_length must be >= 1")`, and with the parameter
    declared a bare `int` that reached the catch-all handler: every one of
    these answered **500** before the bound was added."""
    instrument_id = await _seed_instrument_with_candles()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(
                f"/charts/{instrument_id}/smc?timeframe=5m&swing_length={swing_length}",
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert any(d["loc"] == ["query", "swing_length"] for d in r.json()["detail"])
        finally:
            await _cleanup(user_id)
            await _drop_instrument(instrument_id)


async def test_an_absurd_swing_length_is_capped(require_infra):
    """Behavioural proof for the *upper* bound, which is a cost bound
    rather than a correctness one.

    A huge `swing_length` empties the pivot range and returns 200, so
    nothing looks wrong from the outside -- but values short of that
    simply make the scan expensive. Measured over 16000 candles,
    `detect_swings` costs 26ms at the default 3 and 3976ms at 4000: a
    ~150x multiplier bought with one integer in a query string, on the
    event loop every other request shares.
    """
    instrument_id = await _seed_instrument_with_candles()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(
                f"/charts/{instrument_id}/smc?timeframe=5m&swing_length={10 ** 9}", headers=headers
            )
            assert r.status_code == 422, r.text
            assert any(d["loc"] == ["query", "swing_length"] for d in r.json()["detail"])
        finally:
            await _cleanup(user_id)
            await _drop_instrument(instrument_id)


async def test_an_in_range_swing_length_still_reaches_the_engine(require_infra):
    """Control, and a live one: the bound must reject bad values rather
    than all of them, and the parameter must still change the analysis.

    Both values here are inside the new bounds. Over 200 bars a pivot of
    3 finds swings and so yields a premium/discount dealing range, while
    a pivot of 95 leaves `range(95, 105)` -- too few bars to confirm one
    -- and yields none. If `swing_length` were ignored the two responses
    would be identical, so this fails on a bound that quietly clamps as
    well as on one that rejects everything.
    """
    instrument_id = await _seed_instrument_with_candles()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            tight = client.get(
                f"/charts/{instrument_id}/smc?timeframe=5m&swing_length=3", headers=headers
            )
            wide = client.get(
                f"/charts/{instrument_id}/smc?timeframe=5m&swing_length=95", headers=headers
            )
            assert tight.status_code == 200, tight.text
            assert wide.status_code == 200, wide.text
            assert tight.json()["dealing_range"] is not None
            assert wide.json()["dealing_range"] is None
        finally:
            await _cleanup(user_id)
            await _drop_instrument(instrument_id)


@pytest.mark.parametrize(
    ("field", "value"),
    [("swing_length", 0), ("swing_length", -1), ("starting_balance", -1.0), ("starting_balance", 0.0)],
)
async def test_replay_rejects_the_same_malformed_knobs(require_infra, field, value):
    """Behavioural proof that the *request* is refused, and an honest
    caveat: unlike the charts overlay, `POST /replay` did **not** 500 on
    `swing_length=0`, because `ReplayEngine.analyze` has no caller in
    `app/` and the `SMCConfig` built from this field is therefore never
    used. The value was accepted and stored on the engine regardless.
    This bound is the trap being closed before it is stepped in, not a
    live crash being fixed -- see the request model's own comment.
    """
    instrument_id = await _seed_instrument_with_candles()
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.post(
                "/replay",
                json={
                    "instrument_id": str(instrument_id),
                    "timeframe": "5m",
                    field: value,
                },
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert any(d["loc"] == ["body", field] for d in r.json()["detail"])
        finally:
            await _cleanup(user_id)
            await _drop_instrument(instrument_id)
