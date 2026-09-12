"""Client-supplied strings must fit the columns they land in.

Postgres does not silently truncate an over-long value into a
`VARCHAR(n)` -- it raises `StringDataRightTruncation`. Every string field
below was declared as a bare `str`, so the check happened in the database
rather than at the boundary, and what a client got back for a bad request
was a **500**:

| request | field | column | before |
|---|---|---|---|
| `POST /instruments` | `symbol` at 65 chars | `String(64)` | 500 |
| `POST /instruments` | `exchange` at 33 | `String(32)` | 500 |
| `POST /instruments` | `instrument_type` at 33 | `String(32)` | 500 |
| `POST /instruments` | `currency` at 9 | `String(8)` | 500 |
| `POST /strategies` | `name` at 256 | `String(255)` | 500 |

Two values were accepted that should not have been. An **empty** `symbol`
registered an instrument at 201 -- and `symbol` is the key every lookup in
the system goes through, with a unique index on it, so the empty string is
a real row that can be created exactly once and matches nothing anyone
would search for. An empty strategy `name` was accepted the same way.

The last case is a different failure with the same cause. A
`StrategyDefinition.timeframe` longer than 8 characters was accepted and
stored, and could then never be evaluated: `candles.timeframe` is
`String(8)`, so no candle row can carry a longer string (measured: 8
characters store, 9 raise), and every consumer of the field --
`ScannerWorker`, `AutoTradeSupervisor`, replay, backtest -- loads candles
by that exact string. The strategy simply never fired. That is the rule
the entry-type and condition-type validators already apply: the DSL does
not accept a strategy the engine can never satisfy.

`StrategyDefinition.market` is deliberately left unbounded and is asserted
so below: it has no reader anywhere in `app/` and lands only in the JSON
`definition` column.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument
from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register(client: TestClient, label: str) -> tuple[dict, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


def _strategy_body(**overrides) -> dict:
    body = {
        "name": "Bullish FVG retest",
        "market": "NIFTY",
        "timeframe": "15m",
        "direction": "bullish",
        "conditions": [{"type": "fvg", "direction": "bullish"}],
        "entry": {"type": "fvg_retest"},
        "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
    }
    body.update(overrides)
    return body


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        strategy_ids = (
            await db.execute(select(StrategyRow.id).where(StrategyRow.user_id == user_id))
        ).scalars().all()
        for strategy_id in strategy_ids:
            await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _instrument_count(symbol: str) -> int:
    async with async_session_factory() as db:
        return len(
            (await db.execute(select(Instrument).where(Instrument.symbol == symbol))).scalars().all()
        )


async def test_an_instrument_field_too_long_for_its_column_is_a_bad_request(require_infra):
    """Behavioural proof: each of these answered 500 before, from the
    database rather than the boundary."""
    with TestClient(app) as client:
        headers, user_id = await _register(client, "strinst")
        try:
            base = {"symbol": f"OK{uuid.uuid4().hex[:6].upper()}", "exchange": "NSE",
                    "market": "EQUITY", "instrument_type": "EQ"}
            too_long = {
                "symbol": "S" * 65,
                "exchange": "E" * 33,
                "instrument_type": "T" * 33,
                "currency": "C" * 9,
                "underlying": "U" * 65,
            }
            for field, value in too_long.items():
                body = {**base, "symbol": f"OK{uuid.uuid4().hex[:6].upper()}", field: value}
                r = client.post("/instruments", json=body, headers=headers)
                assert r.status_code == 422, f"{field} -> {r.status_code}: {r.text[:200]}"
                assert field in r.text, f"the 422 for {field} does not name the field: {r.text[:200]}"
        finally:
            await _cleanup(user_id)


async def test_an_instrument_cannot_be_registered_without_a_symbol(require_infra):
    """Behavioural proof. `symbol` carries a unique index and is the key
    every instrument lookup goes through; the empty string used to be a
    perfectly registerable instrument."""
    with TestClient(app) as client:
        headers, user_id = await _register(client, "strempty")
        try:
            # Counted before and after rather than asserted at zero: the
            # empty symbol is a single global row (unique index), so an
            # absolute assertion would couple this test to whatever else
            # has ever touched the table.
            before = await _instrument_count("")
            r = client.post(
                "/instruments",
                json={"symbol": "", "exchange": "NSE", "market": "EQUITY", "instrument_type": "EQ"},
                headers=headers,
            )
            assert r.status_code == 422, r.text
            assert await _instrument_count("") == before, "an empty-symbol instrument reached the table"
        finally:
            await _cleanup(user_id)


async def test_a_strategy_name_too_long_for_its_column_is_a_bad_request(require_infra):
    """Behavioural proof: 256 characters answered 500 before, and an empty
    name was accepted."""
    with TestClient(app) as client:
        headers, user_id = await _register(client, "strname")
        try:
            r = client.post("/strategies", json=_strategy_body(name="N" * 256), headers=headers)
            assert r.status_code == 422, f"long name -> {r.status_code}: {r.text[:200]}"

            r = client.post("/strategies", json=_strategy_body(name=""), headers=headers)
            assert r.status_code == 422, f"empty name -> {r.status_code}: {r.text[:200]}"

            async with async_session_factory() as db:
                rows = (
                    await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))
                ).scalars().all()
            assert rows == [], "a strategy was stored despite the rejection"
        finally:
            await _cleanup(user_id)


async def test_a_strategy_cannot_name_a_timeframe_no_candle_can_carry(require_infra):
    """Behavioural proof of the 'never fires' half.

    A 9-character timeframe is not merely unusual: `candles.timeframe` is
    `String(8)`, so no row in that table can ever hold one, and every
    consumer of this field loads candles by the exact string. The strategy
    was accepted, stored, and silently never evaluated.

    The boundary is asserted from both sides so the bound is exact rather
    than approximately right: 8 characters is still accepted.
    """
    with TestClient(app) as client:
        headers, user_id = await _register(client, "strtf")
        try:
            r = client.post("/strategies", json=_strategy_body(timeframe="T" * 9), headers=headers)
            assert r.status_code == 422, f"9-char timeframe -> {r.status_code}: {r.text[:200]}"

            r = client.post("/strategies", json=_strategy_body(timeframe="T" * 8), headers=headers)
            assert r.status_code == 201, (
                f"8 characters is the column width and must still be accepted: {r.text[:200]}"
            )
            assert r.json()["definition"]["timeframe"] == "T" * 8
        finally:
            await _cleanup(user_id)


async def test_ordinary_values_are_untouched(require_infra):
    """Control. Every bound here is a column width, not a policy, so a
    realistic registration and a realistic strategy must be unaffected --
    including an instrument with a long-but-legal 64-character symbol,
    which is the exact boundary the first test pushes one past.
    """
    symbol = ("LONG" + uuid.uuid4().hex.upper())[:64]
    assert len(symbol) == 36, "fixture: the padded symbol should be well inside the bound"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "strok")
        try:
            r = client.post(
                "/instruments",
                json={"symbol": symbol, "exchange": "NSE", "market": "EQUITY",
                      "instrument_type": "EQ", "currency": "INR"},
                headers=headers,
            )
            assert r.status_code == 201, r.text

            edge = "E" * 64
            r = client.post(
                "/instruments",
                json={"symbol": edge, "exchange": "N" * 32, "market": "EQUITY",
                      "instrument_type": "I" * 32, "currency": "C" * 8},
                headers=headers,
            )
            assert r.status_code == 201, f"a value exactly at the column width must be accepted: {r.text[:200]}"

            r = client.post("/strategies", json=_strategy_body(name="N" * 255), headers=headers)
            assert r.status_code == 201, f"255 characters is the column width: {r.text[:200]}"

            # `market` has no reader and no column width -- it is left
            # unbounded on purpose, and this records that as a decision
            # rather than an oversight.
            r = client.post("/strategies", json=_strategy_body(market="M" * 300), headers=headers)
            assert r.status_code == 201, r.text
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(Instrument).where(Instrument.symbol.in_([symbol, "E" * 64])))
                await db.commit()
            await _cleanup(user_id)
