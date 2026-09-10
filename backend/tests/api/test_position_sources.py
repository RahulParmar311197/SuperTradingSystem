"""`positions` rows are per-engine, not per (user, instrument, mode).

`positions` is a DB mirror of an in-memory `PositionManager`, and three
unrelated ones write into it: the manual stack in `app/api/orders.py`,
each `PaperTradingEngine` behind `POST /paper`, and `AutoTradeSupervisor`
in the worker process. They share no state -- they cannot, the supervisor
being a separate process -- and all of them persist under
`ExecutionMode.PAPER` whenever no broker is connected, which is every
account's default.

Every existing positions assertion in the suite is single-writer and uses
`select(Position).where(Position.user_id == ...)` followed by
`.scalar_one()` -- the assertion itself presumes exactly one row, so no
fixture could reach the collision.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position, Trade
from app.database.models.users import BrokerAccount, User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio

# Opens on the last candle (the FVG retest) and leaves the position open.
_SETUP = [
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


async def _make_instrument(symbol: str) -> uuid.UUID:
    async with async_session_factory() as db:
        instrument = Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument.id


def _create_strategy(client: TestClient, headers: dict, symbol: str) -> uuid.UUID:
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
    return uuid.UUID(r.json()["id"])


def _feed(client: TestClient, headers: dict, session_id: str, candles=_SETUP) -> None:
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    for i, (o, high, low, close) in enumerate(candles):
        r = client.post(
            f"/paper/{session_id}/candle",
            json={
                "timestamp": (start + timedelta(minutes=i)).isoformat(),
                "open": o, "high": high, "low": low, "close": close, "volume": 10,
            },
            headers=headers,
        )
        assert r.status_code == 200, r.text


async def _open_rows(user_id: uuid.UUID) -> list[tuple[str, float, float]]:
    async with async_session_factory() as db:
        rows = (
            await db.execute(select(Position).where(Position.user_id == user_id, Position.is_open.is_(True)))
        ).scalars().all()
    return sorted((r.source_key, float(r.quantity), float(r.average_price)) for r in rows)


async def _cleanup(user_ids: list[uuid.UUID], strategy_ids: list[uuid.UUID], instrument_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        for user_id in user_ids:
            order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
            if order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
            for model in (Trade, Order, Position, RiskEvent, Notification, AuditLog, UserSession, BrokerAccount):
                await db.execute(delete(model).where(model.user_id == user_id))
        for strategy_id in strategy_ids:
            await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id))
        if strategy_ids:
            await db.execute(delete(StrategyRow).where(StrategyRow.id.in_(strategy_ids)))
        for user_id in user_ids:
            await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_a_manual_order_does_not_overwrite_a_paper_sessions_position(require_infra):
    # Regression test: both write `ExecutionMode.PAPER` rows (no broker is
    # connected, the default), and the lookup key was (user, instrument,
    # execution_mode, is_open) -- so `POST /orders` found the paper
    # session's row and overwrote it in place. Pre-fix, the second
    # `_open_rows` below returned a single row `(100.0, 100.0)` and
    # `GET /portfolio.total_exposure` reported 10000.0: the paper
    # session's 15606.06 vanished from `positions`, `GET /portfolio` and
    # the correlated-exposure risk check, which is precisely what
    # `persist_position` was added to make visible.
    symbol = f"SRCA{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "srcmanual")
        instrument_id = await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = client.post(
                "/paper", json={"strategy_id": str(strategy_id), "symbol": symbol}, headers=headers
            ).json()["session_id"]
            _feed(client, headers, session_id)

            paper_rows = await _open_rows(user_id)
            assert len(paper_rows) == 1
            paper_key, paper_qty, paper_price = paper_rows[0]
            assert paper_key == f"paper:{session_id}"
            assert paper_qty > 0
            paper_exposure = client.get("/portfolio", headers=headers).json()["total_exposure"]

            placed = client.post(
                "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0}, headers=headers
            )
            assert placed.status_code == 201, placed.text
            manual_qty = placed.json()["quantity"]

            rows = await _open_rows(user_id)
            assert len(rows) == 2, "the manual order must not overwrite the paper session's row"
            assert (paper_key, paper_qty, paper_price) in rows
            assert ("manual", manual_qty, 100.0) in rows

            # Exposure is now the sum, not whichever engine wrote last.
            exposure = client.get("/portfolio", headers=headers).json()["total_exposure"]
            assert exposure == pytest.approx(paper_exposure + manual_qty * 100.0, rel=1e-6)
            assert exposure > paper_exposure
        finally:
            await _cleanup([user_id], [strategy_id], instrument_id)


async def test_two_paper_sessions_on_one_instrument_keep_separate_positions(require_infra):
    # Each session has its own `PaperTradingEngine`, its own
    # `PositionManager` and its own `MockBroker` balance, so one row
    # cannot represent both. `create_paper_session` has no guard against a
    # second session on an instrument the user already trades, and needs
    # none once the rows are keyed per session.
    symbol = f"SRCB{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "srcpaper")
        instrument_id = await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            first = client.post(
                "/paper", json={"strategy_id": str(strategy_id), "symbol": symbol}, headers=headers
            ).json()["session_id"]
            second = client.post(
                "/paper",
                json={"strategy_id": str(strategy_id), "symbol": symbol, "starting_balance": 250_000.0},
                headers=headers,
            ).json()["session_id"]
            assert first != second

            _feed(client, headers, first)
            _feed(client, headers, second)

            rows = await _open_rows(user_id)
            assert len(rows) == 2
            assert {key for key, _, _ in rows} == {f"paper:{first}", f"paper:{second}"}
            # The larger starting balance sizes a larger position, so the
            # two rows are genuinely different -- not one value written
            # twice.
            quantities = {key: qty for key, qty, _ in rows}
            assert quantities[f"paper:{second}"] > quantities[f"paper:{first}"]
        finally:
            await _cleanup([user_id], [strategy_id], instrument_id)


async def test_repeated_writes_from_one_source_still_update_a_single_row(require_infra):
    # The control: `persist_position` must still be an upsert per source,
    # not an insert per call -- otherwise a paper session would accumulate
    # a row per candle.
    symbol = f"SRCC{uuid.uuid4().hex[:6].upper()}"
    with TestClient(app) as client:
        headers, user_id = await _register(client, "srcsingle")
        instrument_id = await _make_instrument(symbol)
        strategy_id = _create_strategy(client, headers, symbol)
        try:
            session_id = client.post(
                "/paper", json={"strategy_id": str(strategy_id), "symbol": symbol}, headers=headers
            ).json()["session_id"]
            _feed(client, headers, session_id)

            # Several more candles that leave the position open.
            start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc) + timedelta(minutes=len(_SETUP))
            for i in range(3):
                r = client.post(
                    f"/paper/{session_id}/candle",
                    json={
                        "timestamp": (start + timedelta(minutes=i)).isoformat(),
                        "open": 104, "high": 105, "low": 103, "close": 104, "volume": 10,
                    },
                    headers=headers,
                )
                assert r.status_code == 200, r.text

            async with async_session_factory() as db:
                rows = (
                    await db.execute(select(Position).where(Position.user_id == user_id))
                ).scalars().all()
            assert len(rows) == 1
        finally:
            await _cleanup([user_id], [strategy_id], instrument_id)


async def test_the_database_refuses_two_open_rows_for_one_source(require_infra):
    # The endpoint-level keying above cannot be the guarantee on its own;
    # the partial unique index is. Two *closed* rows for one source stay
    # legal, which is what lets a source open a fresh position after its
    # previous one flattened.
    symbol = f"SRCD{uuid.uuid4().hex[:6].upper()}"
    instrument_id = await _make_instrument(symbol)
    user_id = None
    try:
        async with async_session_factory() as db:
            user = User(email=f"srcidx-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", name="srcidx")
            db.add(user)
            await db.commit()
            await db.refresh(user)
            user_id = user.id

        def _row(is_open: bool) -> Position:
            return Position(
                user_id=user_id,
                instrument_id=instrument_id,
                execution_mode=ExecutionMode.PAPER,
                source_key="manual",
                quantity=10,
                average_price=100,
                is_open=is_open,
            )

        async with async_session_factory() as db:
            db.add(_row(is_open=True))
            await db.commit()

        with pytest.raises(IntegrityError):
            async with async_session_factory() as db:
                db.add(_row(is_open=True))
                await db.commit()

        # Closed rows are unconstrained.
        async with async_session_factory() as db:
            db.add(_row(is_open=False))
            db.add(_row(is_open=False))
            await db.commit()
    finally:
        async with async_session_factory() as db:
            if user_id is not None:
                await db.execute(delete(Position).where(Position.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
            await db.commit()
