"""A `/paper` sandbox was reported as the account's own portfolio.

`GET /portfolio` answers "what does this account hold and what has it
made". A `POST /paper` session is a simulation the user creates with its
own `starting_balance`, consuming none of the account's capital -- PR #179
excluded it from the exposure GATE for exactly that reason. The reported
figures never got the same treatment.

Measured through the real endpoints, one account with one real 50,000
position plus one live sandbox session:

    total_exposure     : 50000.00 -> 65606.06   (+15606.06 simulated)
    total_realized_pnl :  5000.00 ->  6000.00   (+1000.00 simulated)

ROUND 86 SAW HALF OF THIS. Its note in `app/api/paper.py::delete_session`
records driving `total_exposure` to "15606 -> 31212 -> 46818 for an
account holding nothing" -- the same 15606 measured here -- and fixed it
by retiring the mirror row when a session is DELETED. That closed one way
in. The rows count the same while the session is alive and entirely
legitimate, which is the case here. Round 86 also wrote "No `Trade` is
journaled: the simulation was abandoned", which is true of an abandoned
session and is why the realized-P&L half went unseen: a sandbox that runs
to target journals a `trades` row like any other close.

As round 86 recorded, this is a reported number and never a bypassed
control -- `RiskEngine.current_exposure` comes from the in-memory
`PositionManager`, not this table.

THE TWO FILTERS HAVE OPPOSITE POLARITY, DELIBERATELY.
`test_a_trade_with_no_journal_source_is_still_counted` is what pins that:
positions use an allowlist over the named, closed partition set, while
trades name only the sandbox, because `journal.source` has a legacy NULL
state that must stay counted.
"""

import ast
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Direction, Strategy as StrategyRow, StrategyVersion
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.trading.persistence import (
    ACCOUNT_BACKED_SOURCE_KEYS,
    AUTO_TRADE_SOURCE,
    MANUAL_TRADE_SOURCE,
    PAPER_SANDBOX_TRADE_SOURCE,
)

SETUP = [
    (100, 100, 99, 100), (100, 102, 100, 101), (101, 103, 100, 102), (102, 102, 97, 98),
    (98, 99, 96, 97), (97, 100, 96, 99), (99, 108, 99, 107), (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces into the FVG -> entry
    (104, 130, 104, 128),  # runs to target -> close, journalling a trade
]

STRATEGY_DEFINITION = {
    "name": "Bullish FVG retest",
    "market": "TESTSYM",
    "timeframe": "15m",
    "direction": "bullish",
    "conditions": [{"type": "fvg", "direction": "bullish"}],
    "entry": {"type": "fvg_retest"},
    "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
}


def _candles():
    start = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)
    return [(start + timedelta(minutes=i), o, h, l, c) for i, (o, h, l, c) in enumerate(SETUP)]


async def _equity() -> Instrument:
    async with async_session_factory() as db:
        row = Instrument(symbol=f"PS{uuid.uuid4().hex[:8].upper()}", exchange="NSE", active=True,
                         market=MarketType.EQUITY, instrument_type="EQ")
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def _user(client) -> tuple[uuid.UUID, dict]:
    email = f"ps-{uuid.uuid4().hex[:8]}@example.com"
    async with async_session_factory() as db:
        row = User(id=uuid.uuid4(), email=email, password_hash=hash_password("testpass123"),
                   name="Sandbox Isolation", trading_permissions=[TradingPermission.LIVE_TRADE.value])
        db.add(row)
        await db.commit()
        user_id = row.id
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    return user_id, {"Authorization": f"Bearer {token}"}


def _open_real_position(client, headers, instrument: Instrument) -> None:
    r = client.post("/orders", headers=headers, json={
        "symbol": instrument.symbol, "direction": "LONG", "order_type": "MARKET",
        "entry": 100.0, "stop": 99.0})
    assert r.status_code == 201, r.text


def _run_sandbox(client, headers, instrument: Instrument, *, bars: int) -> str:
    """Open a `/paper` session and feed it `bars` candles. 9 bars opens a
    position; 10 also closes it, journalling a trade."""
    created = client.post("/strategies", headers=headers, json={
        **STRATEGY_DEFINITION, "name": f"Sandbox {uuid.uuid4().hex[:6]}", "market": instrument.symbol})
    assert created.status_code in (200, 201), created.text
    session = client.post("/paper", headers=headers, json={
        "strategy_id": created.json()["id"], "symbol": instrument.symbol, "timeframe": "15m"})
    assert session.status_code == 200, session.text
    session_id = session.json()["session_id"]
    for ts, o, h, l, c in _candles()[:bars]:
        client.post(f"/paper/{session_id}/candle", headers=headers, json={
            "timestamp": ts.isoformat(), "open": o, "high": h, "low": l, "close": c, "volume": 50000.0})
    return session_id


async def _write_position(user_id: uuid.UUID, instrument_id: uuid.UUID, *, source_key: str,
                          quantity: float, price: float) -> None:
    async with async_session_factory() as db:
        db.add(Position(
            user_id=user_id, instrument_id=instrument_id, execution_mode=ExecutionMode.PAPER,
            quantity=quantity, average_price=price, is_open=True, source_key=source_key,
        ))
        await db.commit()


async def _write_trade(user_id: uuid.UUID, instrument_id: uuid.UUID, *, pnl: float,
                       source: str | None) -> None:
    now = datetime.now(timezone.utc)
    async with async_session_factory() as db:
        db.add(TradeRow(
            user_id=user_id, instrument_id=instrument_id, execution_mode=ExecutionMode.PAPER,
            direction=Direction.LONG, entry_price=100.0, exit_price=110.0, quantity=10.0,
            pnl=pnl, opened_at=now, closed_at=now,
            journal={} if source is None else {"source": source},
        ))
        await db.commit()


async def _sandbox_rows(user_id: uuid.UUID) -> tuple[int, int]:
    """(open sandbox positions, sandbox trades) actually in the DB."""
    async with async_session_factory() as db:
        positions = (await db.execute(select(Position).where(
            Position.user_id == user_id, Position.is_open.is_(True)))).scalars().all()
        trades = (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
    return (
        len([p for p in positions if p.source_key.startswith("paper:")]),
        len([t for t in trades if (t.journal or {}).get("source") == PAPER_SANDBOX_TRADE_SOURCE]),
    )


async def _cleanup(user_id: uuid.UUID, instruments: list[Instrument]) -> None:
    from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS

    async with async_session_factory() as db:
        order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
        if order_ids:
            await db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
        await db.execute(delete(TradeRow).where(TradeRow.user_id == user_id))
        await db.execute(delete(Order).where(Order.user_id == user_id))
        await db.execute(delete(Position).where(Position.user_id == user_id))
        strategy_ids = (await db.execute(
            select(StrategyRow.id).where(StrategyRow.user_id == user_id))).scalars().all()
        if strategy_ids:
            await db.execute(delete(StrategyVersion).where(StrategyVersion.strategy_id.in_(strategy_ids)))
        for model in (Notification, RiskEvent, AuditLog, UserSession, StrategyRow):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        for row in instruments:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == row.id))
            await db.execute(delete(Instrument).where(Instrument.id == row.id))
        await db.commit()
    for cache in (_STACKS, _STACK_LOCKS, _TRADE_LOCKS):
        cache.pop(user_id, None)


# --- the finding ----------------------------------------------------------


async def test_a_live_sandbox_position_is_not_account_exposure(require_infra):
    """The headline. Not an orphaned row -- a healthy, undeleted session."""
    real, sim = await _equity(), await _equity()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            _open_real_position(client, headers, real)
            before = client.get("/portfolio", headers=headers).json()["total_exposure"]

            _run_sandbox(client, headers, sim, bars=9)
            after = client.get("/portfolio", headers=headers).json()["total_exposure"]

            sandbox_positions, _ = await _sandbox_rows(user_id)
            # Non-vacuity: the sandbox must really hold a position, or
            # "the number did not move" says nothing.
            assert sandbox_positions == 1, "the sandbox must have opened a position"

            assert before == 50000.0, "the real position is the whole of the account's exposure"
            assert after == before, "a simulation must not be reported as account exposure"
        finally:
            await _cleanup(user_id, [real, sim])


async def test_a_sandbox_trade_is_not_account_realized_pnl(require_infra):
    """The other half. A sandbox that runs to target journals a `trades`
    row like any other close -- which is why round 86, looking only at
    abandoned sessions, could not see it."""
    real, sim = await _equity(), await _equity()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            _open_real_position(client, headers, real)
            client.post("/orders", headers=headers, json={
                "symbol": real.symbol, "direction": "SHORT", "order_type": "MARKET",
                "entry": 110.0, "stop": 111.0})
            before = client.get("/portfolio", headers=headers).json()["total_realized_pnl"]

            _run_sandbox(client, headers, sim, bars=10)
            after = client.get("/portfolio", headers=headers).json()["total_realized_pnl"]

            _, sandbox_trades = await _sandbox_rows(user_id)
            assert sandbox_trades == 1, "the sandbox must have journalled a closed trade"

            assert before == 5000.0, "the real round trip realized 5,000"
            assert after == before, "simulated profit must not be reported as the account's"
        finally:
            await _cleanup(user_id, [real, sim])


async def test_an_auto_traded_position_is_still_account_exposure(require_infra):
    """Control for the positions filter. An over-fix that narrows to
    `source_key == "manual"` passes the headline and fails this: the
    autonomous engine trades the account's real capital."""
    real = await _equity()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            _open_real_position(client, headers, real)
            before = client.get("/portfolio", headers=headers).json()["total_exposure"]

            await _write_position(user_id, real.id, source_key="auto", quantity=100.0, price=20.0)
            after = client.get("/portfolio", headers=headers).json()["total_exposure"]

            assert after - before == 2000.0, "an auto-traded position is the account's exposure"
        finally:
            await _cleanup(user_id, [real])


async def test_an_auto_trade_pnl_is_still_counted(require_infra):
    """Control for the trades filter, in the other direction: an allowlist
    naming only `manual_order` would drop the autonomous path's P&L."""
    real = await _equity()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            await _write_trade(user_id, real.id, pnl=250.0, source=MANUAL_TRADE_SOURCE)
            before = client.get("/portfolio", headers=headers).json()["total_realized_pnl"]

            await _write_trade(user_id, real.id, pnl=750.0, source=AUTO_TRADE_SOURCE)
            after = client.get("/portfolio", headers=headers).json()["total_realized_pnl"]

            assert before == 250.0
            assert after == 1000.0, "autonomous P&L is the account's P&L"
        finally:
            await _cleanup(user_id, [real])


async def test_a_trade_with_no_journal_source_is_still_counted(require_infra):
    """This is WHY the trades filter is a denylist while the positions
    filter is an allowlist.

    `journal.source` has a legacy NULL state -- rows written before
    `AUTO_TRADE_SOURCE` existed, which `_written_by_the_auto_trade_worker`
    already special-cases for the risk counters. An allowlist here would
    silently drop them and under-report realized P&L, which is the unsafe
    direction for a number a user reads as their own.
    """
    real = await _equity()
    with TestClient(app) as client:
        user_id, headers = await _user(client)
        try:
            await _write_trade(user_id, real.id, pnl=400.0, source=None)
            total = client.get("/portfolio", headers=headers).json()["total_realized_pnl"]

            assert total == 400.0, "a row with no source is not a sandbox row and must still count"
        finally:
            await _cleanup(user_id, [real])


def test_both_portfolio_readers_go_through_the_one_implementation():
    """Structural, and labelled as such: it asserts the SET of readers.

    `GET /portfolio` and the persisted `portfolio_snapshots` both report
    these figures, so a second implementation would reintroduce the bug in
    the place nobody looks. Both must call `compute_portfolio_exposure`,
    and it must be the only thing summing `Position.quantity` for a
    reported total.
    """
    root = pathlib.Path(__file__).resolve().parents[2] / "app"
    readers = [root / "api" / "portfolio.py", root / "trading" / "portfolio_snapshots.py"]

    for path in readers:
        tree = ast.parse(path.read_text())
        calls = {
            n.func.id for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "compute_portfolio_exposure" in calls, f"{path.name} must not compute its own total"

    # And the one implementation really does filter on the named set,
    # rather than merely importing it (round 159 shipped that escape).
    source = (root / "risk" / "portfolio.py").read_text()
    assert "Position.source_key.in_(ACCOUNT_BACKED_SOURCE_KEYS)" in source
    assert "is_distinct_from(PAPER_SANDBOX_TRADE_SOURCE)" in source
    assert ACCOUNT_BACKED_SOURCE_KEYS == ("manual", "auto")
