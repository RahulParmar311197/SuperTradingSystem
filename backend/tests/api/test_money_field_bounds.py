"""Client-supplied money must be money before it reaches the engine.

`POST /replay` bounds its `starting_balance` (`gt=0, lt=1e12`); its two
siblings did not. `POST /paper`'s `starting_balance` and both of
`app/api/backtest.py`'s `starting_capital` fields were bare `float`
defaults, which in Pydantic accept `NaN`, `Infinity` and any magnitude --
and `json.loads` accepts the bare `NaN` / `Infinity` tokens, so no exotic
client is needed to send them. The same held for the OHLC fields of
`POST /paper/{id}/candle`.

Measured on the real endpoints before the bounds below:

* `POST /backtest` with `starting_capital` of `1e30`, `Infinity` or `NaN`
  -> **500** (the `backtests` row will not go into `Numeric(18, 6)`).
  With `-5` or `0` -> 200, and a return-on-capital report computed against
  a negative or zero account.
* `POST /paper` with `Infinity` or `NaN` -> **500** at session creation.
* `POST /paper` with `1e15` -> 201, and then the *ninth* candle 500s: the
  position is sized at 1.5e12 units, `persist_position` cannot write that,
  and the session is left with an open position in the engine and **zero
  rows in the journal** -- `GET /paper/{id}` shows the position while
  `GET /portfolio.total_exposure` reads 0.00, for the life of the session.
  That is the same engine/journal divergence `PlaceOrderRequest`'s bounds
  were added to close.
* `POST /paper/{id}/candle` with a `NaN` close, with a position open ->
  200, and `positions.unrealized_pnl` holds a literal `Decimal('NaN')`
  (Postgres `NUMERIC` accepts NaN); the response serialises it as `null`.
  With `Infinity` -> **500**.

A manual paper session's candles come from the client by design -- the
user is driving a simulation and may invent any price they like. The
bounds assert only that the numbers are prices at all.
"""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.trading import Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio

_START = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
# Opens a LONG on the last bar; the tail carries it to target.
_OPENS = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
]
_OPENS_AND_CLOSES = _OPENS + [(104, 130, 104, 128)]

# Every value that is not money. `NaN` fails the *upper* bound rather than
# the lower one -- every comparison against NaN is False, so `NaN < 1e12`
# is False and Pydantic refuses it; that is the mechanism, and it is why a
# bound is enough here and no separate `allow_inf_nan` switch is needed.
_NOT_MONEY = ["1e30", "Infinity", "NaN", "-5.0", "0.0"]


async def _register(client: TestClient, label: str) -> tuple[dict, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _make_instrument(symbol: str, *, with_candles: bool = False) -> uuid.UUID:
    async with async_session_factory() as db:
        instrument = Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        if with_candles:
            await upsert_candles(
                db,
                instrument_id,
                "15m",
                [
                    Candle(_START + timedelta(minutes=15 * i), o, h, low, c, 100)
                    for i, (o, h, low, c) in enumerate(_OPENS_AND_CLOSES * 3)
                ],
            )
        await db.commit()
        return instrument_id


def _create_strategy(client: TestClient, headers: dict, symbol: str) -> str:
    r = client.post(
        "/strategies",
        json={
            "name": "Bullish FVG retest",
            "market": symbol,
            "timeframe": "15m",
            "direction": "bullish",
            "conditions": [{"type": "fvg", "direction": "bullish"}],
            "entry": {"type": "fvg_retest"},
            "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _post_raw(client: TestClient, headers: dict, path: str, body: str):
    """Posts a body Python's own `json.dumps` would emit for these values.

    `NaN` and `Infinity` are not legal JSON, but `json.loads` -- and so
    every JSON parser in this stack -- accepts them, which is exactly why
    they reach a Pydantic `float` field in the first place. Sending the
    raw text keeps the test honest about what a client can actually put on
    the wire.
    """
    return client.post(path, content=body, headers={**headers, "Content-Type": "application/json"})


async def _cleanup(user_ids: list[uuid.UUID], strategy_ids: list[str], instrument_ids: list[uuid.UUID]) -> None:
    async with async_session_factory() as db:
        for user_id in user_ids:
            for model in (Trade, Position, RiskEvent, Notification, AuditLog, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
        for strategy_id in strategy_ids:
            sid = uuid.UUID(strategy_id)
            await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == sid))
            await db.execute(delete(StrategyRow).where(StrategyRow.id == sid))
        for user_id in user_ids:
            await db.execute(delete(User).where(User.id == user_id))
        for instrument_id in instrument_ids:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_a_paper_session_cannot_be_opened_on_impossible_money(require_infra):
    """Behavioural proof. `Infinity` and `NaN` used to 500 here outright;
    `1e15` used to be accepted and cost its 500 nine candles later, with
    the position already open."""
    symbol = f"MBA{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "moneypaper")
        instrument_id = await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            for value in _NOT_MONEY:
                body = f'{{"strategy_id": "{strategy_id}", "symbol": "{symbol}", "starting_balance": {value}}}'
                r = _post_raw(client, headers, "/paper", body)
                assert r.status_code == 422, f"starting_balance={value} -> {r.status_code}: {r.text[:200]}"
        finally:
            await _cleanup([user_id], [strategy_id], [instrument_id])


async def test_a_paper_candle_must_carry_prices(require_infra):
    """Behavioural proof, with a position open so the value reaches the
    journal. A `NaN` close used to answer 200 and leave `Decimal('NaN')`
    in `positions.unrealized_pnl`; `Infinity` used to 500."""
    symbol = f"MBB{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "moneycandle")
        instrument_id = await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = client.post(
                "/paper", json={"strategy_id": strategy_id, "symbol": symbol}, headers=headers
            ).json()["session_id"]
            for i, (o, high, low, close) in enumerate(_OPENS):
                assert client.post(
                    f"/paper/{session_id}/candle",
                    json={
                        "timestamp": (_START + timedelta(minutes=15 * i)).isoformat(),
                        "open": o, "high": high, "low": low, "close": close, "volume": 10,
                    },
                    headers=headers,
                ).status_code == 200

            async def open_row():
                async with async_session_factory() as db:
                    return (
                        await db.execute(select(Position).where(Position.source_key == f"paper:{session_id}"))
                    ).scalars().one()

            assert (await open_row()).is_open is True, "fixture must leave a position open"

            for value in ("NaN", "Infinity", "-1.0", "0.0", "1e30"):
                body = (
                    '{"timestamp": "2026-01-05T12:00:00+00:00", "open": 104, "high": 105, '
                    f'"low": 103, "close": {value}, "volume": 10}}'
                )
                r = _post_raw(client, headers, f"/paper/{session_id}/candle", body)
                assert r.status_code == 422, f"close={value} -> {r.status_code}: {r.text[:200]}"

            unrealized = (await open_row()).unrealized_pnl
            assert unrealized == unrealized, (
                f"positions.unrealized_pnl is NaN ({unrealized!r}): a rejected candle still "
                "reached the journal"
            )
        finally:
            await _cleanup([user_id], [strategy_id], [instrument_id])


async def test_a_backtest_cannot_be_run_on_impossible_capital(require_infra):
    """Behavioural proof for both backtest entry points. The first three
    values 500ed; the last two produced a return-on-capital report against
    an account that cannot exist."""
    symbol = f"MBC{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "moneybacktest")
        instrument_id = await _make_instrument(symbol, with_candles=True)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            common = {
                "strategy_id": strategy_id,
                "instrument_id": str(instrument_id),
                "timeframe": "15m",
                "start_date": _START.isoformat(),
                "end_date": (_START + timedelta(days=60)).isoformat(),
            }
            for path in ("/backtest", "/backtest/validate"):
                for value in _NOT_MONEY:
                    body = json.dumps(common)[:-1] + f', "starting_capital": {value}}}'
                    r = _post_raw(client, headers, path, body)
                    assert r.status_code == 422, (
                        f"{path} starting_capital={value} -> {r.status_code}: {r.text[:200]}"
                    )
        finally:
            await _cleanup([user_id], [strategy_id], [instrument_id])


async def test_a_large_but_real_account_still_trades_and_journals(require_infra):
    """Control, and the one that keeps the bound honest rather than merely
    tight. 9.99e11 is just under the ceiling: the session must open, trade
    a full round trip, and journal every position write -- which is the
    same path `1e15` used to break at candle nine. A bound set low enough
    to be 'safe' would fail here.
    """
    symbol = f"MBD{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "moneylarge")
        instrument_id = await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            r = client.post(
                "/paper",
                json={"strategy_id": strategy_id, "symbol": symbol, "starting_balance": 9.99e11},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            session_id = r.json()["session_id"]

            for i, (o, high, low, close) in enumerate(_OPENS_AND_CLOSES):
                assert client.post(
                    f"/paper/{session_id}/candle",
                    json={
                        "timestamp": (_START + timedelta(minutes=15 * i)).isoformat(),
                        "open": o, "high": high, "low": low, "close": close, "volume": 10,
                    },
                    headers=headers,
                ).status_code == 200, f"candle {i} was refused on a legitimate account"

            async with async_session_factory() as db:
                rows = (
                    await db.execute(select(Position).where(Position.source_key == f"paper:{session_id}"))
                ).scalars().all()
                trades = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalars().all()
            assert len(rows) == 1, "the position was never journaled"
            # The fixture's last bar carries the trade to target, so the
            # surviving row is the closed one -- quantity 0 by definition.
            # What proves every write along the way succeeded is the
            # journaled trade: it carries the size the account actually
            # took, which is the number that broke the `Numeric(18, 6)`
            # column at 1e15.
            assert rows[0].is_open is False
            assert len(trades) == 1, "the round trip was never journaled"
            assert float(trades[0].quantity) > 1e9, (
                f"a 9.99e11 account should size in the billions of units, got "
                f"{trades[0].quantity} -- this fixture is not exercising the range "
                "the bound is about"
            )
        finally:
            await _cleanup([user_id], [strategy_id], [instrument_id])
