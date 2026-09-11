"""`DELETE /paper/{id}` must retire the `positions` row it was mirroring.

`feed_candle` mirrors the paper engine's position into Postgres on every
candle under `source_key=f"paper:{session_id}"`. `close_paper_session` used
to drop the in-memory engine and nothing else, and nothing else *can* clean
up after it: `app/trading/persistence.py` is the only writer of
`Position.is_open` in `app/`, reaching it needs a live engine plus that
`source_key`, and a new session gets a new UUID and therefore a new key. The
row stayed `is_open=True` permanently, unaddressable by anything.

`app/risk/portfolio.py::compute_portfolio_exposure` sums exactly those rows,
so `GET /portfolio.total_exposure` -- and the `portfolio_snapshots` journal
built from it -- grew every time a session was deleted with a position open,
for an account holding nothing.

This is a reporting fault, not a control failure: `RiskEngine`'s
`current_exposure` on both `POST /orders` and `POST /options/execute` is
summed from the in-memory `PositionManager`, never from this table.

Why the existing tests missed it.
`tests/api/test_paper.py::test_create_get_feed_and_close_paper_session` is
the test whose *name* asserts this contract, but its fixture feeds a single
candle and `PaperTradingEngine.on_candle` cannot produce a signal from one
bar -- so no `positions` row is ever written and there is nothing to orphan
(shapes b and e). Its sibling
`test_paper_trading_persists_the_open_position_to_the_database` does reach
an open row and does assert `is_open is False`, but only via the natural
target exit; it never deletes while the position is open. No test in the
suite called DELETE with a position open.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.trading import Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio

# Opens a LONG on the last bar and leaves it open.
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
# The same run, plus the bar that carries it to target and closes it.
_OPENS_AND_CLOSES = _OPENS + [(104, 130, 104, 128)]


async def _register(client: TestClient, label: str) -> tuple[dict, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _make_instrument(symbol: str) -> uuid.UUID:
    async with async_session_factory() as db:
        instrument = Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id


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


def _run_session(client: TestClient, headers: dict, strategy_id: str, symbol: str, candles=_OPENS) -> str:
    session_id = client.post(
        "/paper", json={"strategy_id": strategy_id, "symbol": symbol}, headers=headers
    ).json()["session_id"]
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    for i, (o, high, low, close) in enumerate(candles):
        r = client.post(
            f"/paper/{session_id}/candle",
            json={
                "timestamp": (start + timedelta(minutes=15 * i)).isoformat(),
                "open": o, "high": high, "low": low, "close": close, "volume": 10,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text
    return session_id


async def _rows(user_id: uuid.UUID) -> list[Position]:
    async with async_session_factory() as db:
        return list(
            (await db.execute(select(Position).where(Position.user_id == user_id))).scalars().all()
        )


async def _cleanup(user_ids: list[uuid.UUID], strategy_ids: list[str], symbols: list[str]) -> None:
    async with async_session_factory() as db:
        for user_id in user_ids:
            for model in (Trade, Position, RiskEvent, Notification, AuditLog, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
        for strategy_id in strategy_ids:
            await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == uuid.UUID(strategy_id)))
            await db.execute(delete(StrategyRow).where(StrategyRow.id == uuid.UUID(strategy_id)))
        for user_id in user_ids:
            await db.execute(delete(User).where(User.id == user_id))
        if symbols:
            await db.execute(delete(Instrument).where(Instrument.symbol.in_(symbols)))
        await db.commit()


async def test_deleting_a_session_with_an_open_position_retires_its_mirror(require_infra):
    # Regression test: the row stayed is_open=True forever and nothing could
    # ever address it again, so it counted toward exposure permanently.
    symbol = f"PCA{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "papercleanup")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = _run_session(client, headers, strategy_id, symbol)

            before = await _rows(user_id)
            assert len(before) == 1 and before[0].is_open is True, "fixture must open a real position"
            assert client.get("/portfolio", headers=headers).json()["total_exposure"] > 0

            assert client.delete(f"/paper/{session_id}", headers=headers).status_code == 204

            after = await _rows(user_id)
            assert len(after) == 1, "the row is retired, not deleted"
            assert after[0].is_open is False
            assert after[0].source_key == f"paper:{session_id}"
            assert client.get("/portfolio", headers=headers).json()["total_exposure"] == 0.0
        finally:
            await _cleanup([user_id], [strategy_id], [symbol])


async def test_repeated_create_and_delete_does_not_accumulate_exposure(require_infra):
    # The harm as a user actually meets it: exposure used to climb by a whole
    # position on every cycle and never come back down.
    symbol = f"PCB{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "papercycle")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            readings = []
            for _ in range(3):
                session_id = _run_session(client, headers, strategy_id, symbol)
                client.delete(f"/paper/{session_id}", headers=headers)
                readings.append(client.get("/portfolio", headers=headers).json()["total_exposure"])

            assert readings == [0.0, 0.0, 0.0], f"exposure must not accumulate, got {readings}"
            assert all(r.is_open is False for r in await _rows(user_id))
        finally:
            await _cleanup([user_id], [strategy_id], [symbol])


async def test_the_abandoned_row_keeps_what_the_session_held(require_infra):
    # Retiring is not erasing: the quantity and entry price stay, so the
    # record of what the discarded session was holding survives.
    symbol = f"PCC{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "paperkeep")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = _run_session(client, headers, strategy_id, symbol)
            held = (await _rows(user_id))[0]
            quantity, average_price = float(held.quantity), float(held.average_price)
            assert quantity > 0

            client.delete(f"/paper/{session_id}", headers=headers)

            row = (await _rows(user_id))[0]
            assert float(row.quantity) == pytest.approx(quantity)
            assert float(row.average_price) == pytest.approx(average_price)
            assert float(row.unrealized_pnl) == 0.0, "an abandoned position has no live mark"
        finally:
            await _cleanup([user_id], [strategy_id], [symbol])


async def test_abandoning_a_session_journals_no_trade(require_infra):
    # Nothing was sold at any price. `GET /portfolio.total_realized_pnl` sums
    # the `trades` journal, so inventing an exit would put a fabricated P&L
    # into the account's realized total.
    symbol = f"PCD{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "papernotrade")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = _run_session(client, headers, strategy_id, symbol)
            client.delete(f"/paper/{session_id}", headers=headers)

            async with async_session_factory() as db:
                trades = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalars().all()
            assert trades == []
            assert client.get("/portfolio", headers=headers).json()["total_realized_pnl"] == 0.0
        finally:
            await _cleanup([user_id], [strategy_id], [symbol])


async def test_deleting_one_session_leaves_another_sessions_position_alone(require_infra):
    # The scoping that makes this safe: the lookup is keyed by source_key, so
    # it can only retire the row the deleted session itself wrote.
    symbol = f"PCE{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "papertwo")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            first = _run_session(client, headers, strategy_id, symbol)
            second = _run_session(client, headers, strategy_id, symbol)
            assert len({first, second}) == 2
            exposure_with_both = client.get("/portfolio", headers=headers).json()["total_exposure"]

            client.delete(f"/paper/{first}", headers=headers)

            rows = {r.source_key: r.is_open for r in await _rows(user_id)}
            assert rows[f"paper:{first}"] is False
            assert rows[f"paper:{second}"] is True, "the surviving session still holds its position"

            remaining = client.get("/portfolio", headers=headers).json()["total_exposure"]
            assert remaining > 0
            assert remaining == pytest.approx(exposure_with_both / 2, rel=1e-6)
        finally:
            await _cleanup([user_id], [strategy_id], [symbol])


async def test_a_naturally_closed_position_is_unaffected(require_infra):
    # The control: a session whose position already exited at target must
    # still delete cleanly, and its row must stay closed with the Trade that
    # really happened intact.
    symbol = f"PCF{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "paperclosed")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = _run_session(client, headers, strategy_id, symbol, candles=_OPENS_AND_CLOSES)

            async with async_session_factory() as db:
                trades = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalars().all()
            assert len(trades) == 1, "the position really did exit at target"

            assert client.delete(f"/paper/{session_id}", headers=headers).status_code == 204

            rows = await _rows(user_id)
            assert all(r.is_open is False for r in rows)
            async with async_session_factory() as db:
                still = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalars().all()
            assert len(still) == 1, "the real exit is not disturbed"
        finally:
            await _cleanup([user_id], [strategy_id], [symbol])


async def test_deleting_a_session_that_never_opened_anything_still_succeeds(require_infra):
    # The other control: no mirror was ever written, so there is nothing to
    # retire and the endpoint must not fail looking for one.
    symbol = f"PCG{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "paperempty")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = client.post(
                "/paper", json={"strategy_id": strategy_id, "symbol": symbol}, headers=headers
            ).json()["session_id"]

            assert client.delete(f"/paper/{session_id}", headers=headers).status_code == 204
            assert await _rows(user_id) == []
            assert client.get(f"/paper/{session_id}", headers=headers).status_code == 404
        finally:
            await _cleanup([user_id], [strategy_id], [symbol])


async def test_another_user_cannot_retire_someone_elses_mirror(require_infra):
    symbol = f"PCH{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        owner_headers, owner_id = await _register(client, "paperowner")
        other_headers, other_id = await _register(client, "paperother")
        await _make_instrument(symbol)
        strategy_id = _create_strategy(client, owner_headers, symbol)
        try:
            session_id = _run_session(client, owner_headers, strategy_id, symbol)

            assert client.delete(f"/paper/{session_id}", headers=other_headers).status_code == 404
            assert (await _rows(owner_id))[0].is_open is True
        finally:
            await _cleanup([owner_id, other_id], [strategy_id], [symbol])
