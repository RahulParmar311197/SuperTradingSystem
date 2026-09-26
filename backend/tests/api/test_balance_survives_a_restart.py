"""The account's cash is rebuilt from the journal on every stack build,
and an exit is never sized down to nothing.

Round 170 made `MockBroker`'s balance follow realized P&L (see
tests/brokers/test_mock_broker_balance.py). That turned two things that
had been inert into live defects, and this file covers both.

1. `MockBroker._balance` lives in this process. Rounds 154 and 155
   rebuilt `trades_today`/`daily_pnl`/`weekly_pnl` from the journal
   because a restart must not lift a limit; the DENOMINATOR those three
   are measured against was left behind. Measured through the endpoint,
   a -5,000 realized on a 100,000 account:

       before restart -> 403 "Daily loss 5.26% vs limit 2.0%"
       after  restart -> 403 "Daily loss 5.00% vs limit 2.0%"

   Same loss, same journal, two different numbers, because the loss came
   back from Postgres and the balance it divides by did not. The gap
   widens with every loss, and always in the direction of reporting the
   account as healthier than it is.

2. `calculate_position_size` returns 0 for any balance <= 0, and
   `POST /orders` sizes a REDUCING order from it too. An account whose
   journal totals -100,000 rehydrates to exactly 0, and then:

       POST /orders SHORT entry=99 stop=103 -> 201, quantity 0.0
       positions still open                 -> [100.0]

   A 201 reporting a zero-share fill with the position untouched, and no
   stop tight enough to help because every width divides into a zero
   budget. `POST /orders` is the only way out of a position, so that is
   a user locked into a trade with a success response.
"""

import ast
import pathlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS
from app.auth.security import TokenType, decode_token
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Direction
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.risk.limits import RiskLimits
from app.trading.persistence import (
    AUTO_TRADE_SOURCE,
    MANUAL_TRADE_SOURCE,
    PAPER_SANDBOX_TRADE_SOURCE,
)

LIMITS = RiskLimits()
# MockBroker's starting balance: every account in this file trades against
# it, because none has a connected broker account.
BALANCE = 100_000.0
# Past the 2% daily limit and derived from it, so a change to the limit
# cannot leave these tests asserting something they no longer prove.
LOSING_PNL = -(BALANCE * LIMITS.max_daily_loss_pct / 100) * 1.25


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"bal-{uuid.uuid4().hex[:8]}@example.com"
    assert client.post(
        "/auth/register", json={"email": email, "password": "testpass123", "name": "Bal"}
    ).status_code == 201
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.post(
        "/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers
    ).status_code == 200
    return headers, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _instrument(prefix: str = "BAL") -> tuple[uuid.UUID, str]:
    async with async_session_factory() as db:
        row = Instrument(
            symbol=f"{prefix}{uuid.uuid4().hex[:6].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id, row.symbol


def _restart(user_id: uuid.UUID) -> None:
    """What a process restart does to this user and nothing else: the
    in-memory stack is gone, Postgres is untouched."""
    _STACKS.pop(user_id, None)
    _TRADE_LOCKS.pop(user_id, None)
    _STACK_LOCKS.pop(user_id, None)


async def _journal_row(
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    *,
    pnl: float,
    when: datetime,
    source: str = MANUAL_TRADE_SOURCE,
    execution_mode: ExecutionMode = ExecutionMode.PAPER,
) -> None:
    """A closed trade at a chosen time. `ExecutionMode.PAPER` is what a
    MockBroker stack journals (see `_execution_mode_for`), not LIVE."""
    async with async_session_factory() as db:
        db.add(
            Trade(
                user_id=user_id,
                instrument_id=instrument_id,
                execution_mode=execution_mode,
                direction=Direction.LONG,
                entry_price=100.0,
                exit_price=90.0,
                quantity=10.0,
                pnl=pnl,
                opened_at=when,
                closed_at=when,
                journal={"source": source},
            )
        )
        await db.commit()


async def _balance(user_id: uuid.UUID) -> float:
    return (await _STACKS[user_id].broker.get_account()).balance


async def _open_quantities(user_id: uuid.UUID) -> list[float]:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(Position.quantity).where(Position.user_id == user_id, Position.is_open.is_(True))
            )
        ).scalars().all()
    return [float(q) for q in rows]


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first: order_events -> orders, trades -> positions,
    sessions/audit_logs -> users."""
    async with async_session_factory() as db:
        for user_id in user_ids:
            order_ids = (await db.execute(select(Order.id).where(Order.user_id == user_id))).scalars().all()
            for order_id in order_ids:
                await db.execute(delete(OrderEvent).where(OrderEvent.order_id == order_id))
            for model in (Order, Trade, Position, Notification, RiskEvent, AuditLog, UserSession):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        for instrument_id in instrument_ids:
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()
    for user_id in user_ids:
        _restart(user_id)


# --- 1. the denominator survives a restart --------------------------------


async def test_the_same_loss_gives_the_same_verdict_before_and_after_a_restart(require_infra):
    """The headline. Rounds 154/155 brought the loss back from the journal;
    this brings back the balance it is divided by, so the gate returns the
    same answer either side of a deploy."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            # Realize the loss through the real path: open 100 at 100,
            # close at 50 -> -5,000.
            assert client.post(
                "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            ).status_code == 201
            assert client.post(
                "/orders", json={"symbol": symbol, "direction": "SHORT", "entry": 50.0, "stop": 55.0},
                headers=headers,
            ).status_code == 201
            balance_before = await _balance(user_id)
            assert balance_before == pytest.approx(BALANCE - 5_000.0), "the loss must have moved the cash"

            before = client.post(
                "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert before.status_code == 403, before.text
            assert "Daily loss" in before.text

            _restart(user_id)

            after = client.post(
                "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert after.status_code == 403, after.text
            assert after.json()["detail"] == before.json()["detail"], (
                "a restart changed the account's own loss percentage"
            )
            assert await _balance(user_id) == pytest.approx(balance_before)
        finally:
            await _cleanup([user_id], [instrument_id])


async def test_the_balance_is_rebuilt_from_the_whole_journal_not_just_today(require_infra):
    """`daily_pnl` and `weekly_pnl` have windows; cash does not. A loss
    from last month is gone from the account even though it is outside
    every risk window -- a rebuild that reused `day_start` would report
    the full 100,000 here."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            long_ago = datetime.now(timezone.utc) - timedelta(days=30)
            await _journal_row(user_id, instrument_id, pnl=-40_000.0, when=long_ago)
            _restart(user_id)
            # Any request that builds the stack.
            client.get("/portfolio", headers=headers)

            assert await _balance(user_id) == pytest.approx(BALANCE - 40_000.0)
            stack = _STACKS[user_id]
            assert stack.daily_pnl == pytest.approx(0.0), "a 30-day-old loss is not today's"
            assert stack.weekly_pnl == pytest.approx(0.0), "nor this week's"
        finally:
            await _cleanup([user_id], [instrument_id])


async def test_another_users_losses_do_not_move_this_account(require_infra):
    """Scoping control. Without the `user_id` filter this reads the whole
    trades table, and the shared test database has plenty in it."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        other_headers, other_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _journal_row(other_id, instrument_id, pnl=-40_000.0, when=datetime.now(timezone.utc))
            _restart(user_id)
            client.get("/portfolio", headers=headers)

            assert await _balance(user_id) == pytest.approx(BALANCE)
        finally:
            await _cleanup([user_id, other_id], [instrument_id])


@pytest.mark.parametrize("source", [PAPER_SANDBOX_TRADE_SOURCE, AUTO_TRADE_SOURCE])
async def test_only_this_paths_own_journal_rows_rebuild_the_balance(require_infra, source):
    """The manual stack's cash is rebuilt from the same filter its
    counters use -- `journal.source == manual_order`. A `/paper` sandbox
    (round 165) and the auto-trade worker keep their own accounts, and
    mixing them here would make one path's losses shrink another path's
    risk budget."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _journal_row(
                user_id, instrument_id, pnl=-40_000.0, when=datetime.now(timezone.utc), source=source
            )
            _restart(user_id)
            client.get("/portfolio", headers=headers)

            assert await _balance(user_id) == pytest.approx(BALANCE)
        finally:
            await _cleanup([user_id], [instrument_id])


async def test_a_manual_row_does_move_it(require_infra):
    """Non-vacuity control for the two above: the same row under this
    path's own source is picked up, so those assertions are about the
    filter and not about the rebuild being dead."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _journal_row(
                user_id, instrument_id, pnl=-40_000.0, when=datetime.now(timezone.utc),
                source=MANUAL_TRADE_SOURCE,
            )
            _restart(user_id)
            client.get("/portfolio", headers=headers)

            assert await _balance(user_id) == pytest.approx(BALANCE - 40_000.0)
        finally:
            await _cleanup([user_id], [instrument_id])


def test_only_a_mock_brokers_balance_is_ever_overwritten():
    """A real adapter's balance lives at the broker and is authoritative.
    Replacing it with `starting_balance + our journal sum` would be a
    worse bug than the one this round fixed, so the rebuild sits inside an
    `isinstance(..., MockBroker)` guard. Asserted against the call itself
    rather than the name, so moving the call out of the guard is caught
    even though both strings still appear in the file.
    """
    tree = ast.parse(pathlib.Path("app/api/orders.py").read_text())

    def is_mock_guard(node: ast.expr) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "isinstance"
            and any(isinstance(a, ast.Name) and a.id == "MockBroker" for a in node.args)
        )

    guarded, total = 0, 0
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "restore_realized_pnl"
        ):
            total += 1
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and is_mock_guard(node.test):
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "restore_realized_pnl"
                ):
                    guarded += 1

    assert total == 1, f"expected exactly one rebuild call site, found {total}"
    assert guarded == total, "the balance rebuild must sit inside an isinstance(..., MockBroker) guard"


# --- 2. an exit is never sized down to nothing ----------------------------


async def test_a_wiped_out_account_can_still_close_its_position(require_infra):
    """The headline for the second defect. Balance 0, one open position,
    and the only endpoint that can close it."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument("TRAP")
        try:
            assert client.post(
                "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            ).status_code == 201
            assert await _open_quantities(user_id) == [100.0]

            # A history of losses totalling the whole account, dated well
            # outside today's and this week's windows so the daily gates
            # are not what is under test.
            await _journal_row(
                user_id, instrument_id, pnl=-BALANCE,
                when=datetime.now(timezone.utc) - timedelta(days=30),
            )
            _restart(user_id)
            client.get("/portfolio", headers=headers)
            assert await _balance(user_id) == pytest.approx(0.0)

            close = client.post(
                "/orders", json={"symbol": symbol, "direction": "SHORT", "entry": 99.0, "stop": 103.0},
                headers=headers,
            )
            assert close.status_code == 201, close.text
            assert close.json()["quantity"] == pytest.approx(100.0), (
                "the exit was sized from a zero risk budget"
            )
            assert await _open_quantities(user_id) == []
        finally:
            await _cleanup([user_id], [instrument_id])


async def test_the_floor_does_not_turn_a_partial_close_into_a_full_one(require_infra):
    """The over-fix control, in the other direction. Round 168 established
    that a partial close is a documented capability expressed with a WIDER
    closing stop; a floor that always closed everything would delete it.

    Measured on a flat 100,000 account with 100 open: a 10-wide closing
    stop is 100,000 * 0.5% / 10 = 50 shares, leaving 50 open.
    """
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            assert client.post(
                "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            ).status_code == 201
            assert await _balance(user_id) == pytest.approx(BALANCE), "nothing realized yet"

            close = client.post(
                "/orders", json={"symbol": symbol, "direction": "SHORT", "entry": 100.0, "stop": 110.0},
                headers=headers,
            )
            assert close.status_code == 201, close.text
            assert close.json()["quantity"] == pytest.approx(50.0)
            assert await _open_quantities(user_id) == [pytest.approx(50.0)]
        finally:
            await _cleanup([user_id], [instrument_id])


async def test_an_unfunded_entry_is_still_refused_rather_than_filled_at_zero(require_infra):
    """The floor is for exits only, and this is the absence getting its
    own test. A new position on an account with no money is refused by
    round 168's `account_funded` gate -- it must not fall through to the
    floor, and it must not become a 201 for zero shares either."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        instrument_id, symbol = await _instrument()
        try:
            await _journal_row(
                user_id, instrument_id, pnl=-BALANCE,
                when=datetime.now(timezone.utc) - timedelta(days=30),
            )
            _restart(user_id)
            client.get("/portfolio", headers=headers)
            assert await _balance(user_id) == pytest.approx(0.0)

            entry = client.post(
                "/orders", json={"symbol": symbol, "direction": "LONG", "entry": 100.0, "stop": 95.0},
                headers=headers,
            )
            assert entry.status_code == 403, entry.text
            assert "cannot support a new position" in entry.text
            assert await _open_quantities(user_id) == []
        finally:
            await _cleanup([user_id], [instrument_id])
