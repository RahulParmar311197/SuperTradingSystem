"""A process restart cleared the day's risk counters, lifting the gates.

`_stack_for` (app/api/orders.py) rehydrates a restarted process's open
positions and its order idempotency index from Postgres -- both added in
earlier rounds for one reason: a restart must not lift a limit. The
counters that *are* the daily limits were left behind. `trades_today`,
`daily_pnl` and `weekly_pnl` all started at 0 on every stack build.

Measured through the real endpoint on default limits:

    10 orders placed -> #11 is 403 "10 trades today vs limit 10"
    restart          -> #12 fills 201, trades_today=1, 11 orders journalled

    realized -2500 (2.50% vs the 2.0% daily limit)
                     -> 403 "Daily loss 2.50% vs limit 2.0%"
    restart          -> the same order fills 201, daily_pnl 0.00, against
                        a -2500 row in the trades journal

The second one is the daily circuit breaker -- the gate whose entire job
is to stop an account bleeding out -- being cleared by a deploy, an OOM
kill or a crash loop, and a crash loop clears it again on every pass.

Round 72 fixed the opposite direction: counters that never reset, so a
"daily" limit was really lifetime-of-process. This is the same limit
failing the other way, and its own docs noted in passing that "these
counters only ever cleared on a restart" without treating that as the
defect it is.

The fix rebuilds all three from the journal at stack build, over exactly
the windows `_roll_risk_window` measures.
"""

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import delete, select, update

from app.api.orders import _STACK_LOCKS, _STACKS, _TRADE_LOCKS
from app.auth.security import TokenType, decode_token
from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Direction
from app.database.models.trading import ExecutionMode, Order, OrderEvent, Position, Trade
from app.database.models.users import TradingPermission, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.risk.limits import RiskLimits
from app.trading.persistence import (
    MANUAL_TRADE_SOURCE,
    load_orders_placed_since,
    load_realized_pnl_since,
    risk_window_starts,
)

LIMITS = RiskLimits()
MAX_TRADES = LIMITS.max_trades_per_day
MAX_OPEN = LIMITS.max_open_positions

# MockBroker's starting balance -- what the loss percentages are measured
# against. A manual stack with no connected broker account trades against
# it, which is every account in this suite.
BALANCE = 100_000.0
# Comfortably past the 2% daily limit, and derived from it rather than
# picked, so a change to the limit cannot leave this test asserting
# something it no longer proves.
LOSING_TRADE_PNL = -(BALANCE * LIMITS.max_daily_loss_pct / 100) * 1.25


async def _make_instruments(count: int) -> tuple[list[uuid.UUID], list[str]]:
    ids, symbols = [], []
    async with async_session_factory() as db:
        for _ in range(count):
            instrument = Instrument(
                symbol=f"RSTC{uuid.uuid4().hex[:6].upper()}",
                exchange="NSE",
                market=MarketType.EQUITY,
                instrument_type="EQ",
            )
            db.add(instrument)
            await db.flush()
            ids.append(instrument.id)
            symbols.append(instrument.symbol)
        await db.commit()
    return ids, symbols


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first. `trades` references `positions`, so it has to go
    before it -- deleting positions first raises a foreign key violation
    (measured, not guessed: it is what the first run of this probe did)."""
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


def _restart(user_id: uuid.UUID) -> None:
    """What a process restart does to this user, and nothing else: the
    in-memory stack is gone, Postgres is untouched."""
    _STACKS.pop(user_id, None)
    _TRADE_LOCKS.pop(user_id, None)
    _STACK_LOCKS.pop(user_id, None)


async def _register(client: httpx.AsyncClient) -> tuple[dict, uuid.UUID]:
    email = f"rstc-{uuid.uuid4().hex[:8]}@example.com"
    r = await client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "R"})
    assert r.status_code == 201, r.text
    token = (await client.post("/auth/login", json={"email": email, "password": "testpass123"})).json()["access_token"]
    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    async with async_session_factory() as db:
        await db.execute(
            update(User).where(User.id == user_id).values(trading_permissions=[TradingPermission.LIVE_TRADE.value])
        )
        await db.commit()
    return {"Authorization": f"Bearer {token}"}, user_id


def _open(symbol: str) -> dict:
    return {"symbol": symbol, "direction": "LONG", "order_type": "MARKET", "entry": 100.0, "stop": 95.0}


def _close(symbol: str, price: float) -> dict:
    return {"symbol": symbol, "direction": "SHORT", "order_type": "MARKET", "entry": price, "stop": price + 5}


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _write_trade(
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    *,
    pnl: float,
    closed_at: datetime,
    source: str = MANUAL_TRADE_SOURCE,
    execution_mode: ExecutionMode = ExecutionMode.PAPER,
) -> None:
    """A journal row placed at a chosen time. Used for the windows a live
    request cannot reach -- yesterday, and earlier this week."""
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
                opened_at=closed_at,
                closed_at=closed_at,
                journal={"source": source},
            )
        )
        await db.commit()


# --- the finding ----------------------------------------------------------


async def test_the_daily_order_count_survives_a_restart(require_infra):
    """Behavioural proof for `max_trades_per_day`. Ten orders (five opens
    and five closes -- a closing order is reducing, so it is exempt from
    the position cap but still counts as an order) exhaust the day's
    allowance; a restart used to hand the account a fresh ten."""
    instrument_ids, symbols = await _make_instruments(MAX_OPEN + 1)
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            for symbol in symbols[:MAX_OPEN]:
                assert (await client.post("/orders", json=_open(symbol), headers=headers)).status_code == 201
            for symbol in symbols[:MAX_OPEN]:
                assert (await client.post("/orders", json=_close(symbol, 100.0), headers=headers)).status_code == 201
            assert _STACKS[user_id].trades_today == MAX_TRADES

            before = await client.post("/orders", json=_open(symbols[MAX_OPEN]), headers=headers)
            assert before.status_code == 403, before.text
            assert "trades today" in before.json()["detail"]

            _restart(user_id)

            after = await client.post("/orders", json=_open(symbols[MAX_OPEN]), headers=headers)
            assert after.status_code == 403, f"a restart lifted max_trades_per_day: {after.text}"
            assert after.json()["detail"] == before.json()["detail"]
            assert _STACKS[user_id].trades_today == MAX_TRADES
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_the_daily_loss_limit_survives_a_restart(require_infra):
    """Behavioural proof for the gate that matters most: an account that
    has already lost past its daily limit must not be able to trade again
    just because the process bounced."""
    instrument_ids, symbols = await _make_instruments(3)
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            assert (await client.post("/orders", json=_open(symbols[0]), headers=headers)).status_code == 201
            # Sized at 0.5% risk over a 5-point stop, so this fills 100
            # units; closing 25 points down realizes -2500 = 2.5% of the
            # 100k account, past the 2% daily limit.
            assert (await client.post("/orders", json=_close(symbols[0], 75.0), headers=headers)).status_code == 201
            assert _STACKS[user_id].daily_pnl == pytest.approx(-2500.0)

            before = await client.post("/orders", json=_open(symbols[1]), headers=headers)
            assert before.status_code == 403, before.text
            assert "Daily loss" in before.json()["detail"]

            _restart(user_id)

            after = await client.post("/orders", json=_open(symbols[2]), headers=headers)
            assert after.status_code == 403, f"a restart lifted the daily loss limit: {after.text}"
            assert after.json()["detail"] == before.json()["detail"]
            assert _STACKS[user_id].daily_pnl == pytest.approx(-2500.0)
    finally:
        await _cleanup(user_ids, instrument_ids)


# --- the windows are windows ---------------------------------------------


async def test_yesterdays_orders_and_losses_do_not_count_today(require_infra):
    """The obvious over-fix: summing the whole journal instead of the
    day's. That would make a "daily" limit cumulative-forever, which is
    precisely the bug round 72 fixed from the other side -- so this has to
    be a test, not a comment."""
    instrument_ids, symbols = await _make_instruments(1)
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)
            # One order today, so the stack exists and the instrument is
            # real; everything else is backdated.
            assert (await client.post("/orders", json=_open(symbols[0]), headers=headers)).status_code == 201

            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            await _write_trade(user_id, instrument_ids[0], pnl=LOSING_TRADE_PNL, closed_at=yesterday)
            async with async_session_factory() as db:
                await db.execute(
                    update(Order).where(Order.user_id == user_id).values(created_at=yesterday)
                )
                await db.commit()

            _restart(user_id)
            async with async_session_factory() as db:
                day_start, _ = risk_window_starts(datetime.now(timezone.utc))
                assert await load_orders_placed_since(db, user_id, ExecutionMode.PAPER, since=day_start) == 0
                assert await load_realized_pnl_since(db, user_id, ExecutionMode.PAPER, since=day_start) == 0.0

            # And the account can still trade, which is the point.
            again = await client.post("/orders", json=_open(symbols[0]), headers=headers)
            assert again.status_code == 201, again.text
            assert _STACKS[user_id].trades_today == 1
            assert _STACKS[user_id].daily_pnl == 0.0
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_loss_earlier_this_week_counts_weekly_but_not_daily(require_infra):
    """The two windows are different windows. A fix that measured both
    from the day boundary would lose every weekly loss booked before
    today, and `max_weekly_loss_pct` would only ever see one day."""
    instrument_ids, _ = await _make_instruments(1)
    user_ids = []
    try:
        async with _client() as client:
            _, user_id = await _register(client)
            user_ids.append(user_id)

            now = datetime.now(timezone.utc)
            day_start, week_start = risk_window_starts(now)
            await _write_trade(user_id, instrument_ids[0], pnl=LOSING_TRADE_PNL, closed_at=week_start)

            async with async_session_factory() as db:
                weekly = await load_realized_pnl_since(db, user_id, ExecutionMode.PAPER, since=week_start)
                daily = await load_realized_pnl_since(db, user_id, ExecutionMode.PAPER, since=day_start)

            assert weekly == pytest.approx(LOSING_TRADE_PNL), "a loss booked this week must count weekly"
            if week_start == day_start:
                # Monday: the week starts today, so the two windows
                # genuinely coincide. Asserted rather than skipped, so
                # this test proves something every day of the week.
                assert now.weekday() == 0
                assert daily == pytest.approx(LOSING_TRADE_PNL)
            else:
                assert daily == 0.0, "a loss booked before today must not count toward the daily limit"
    finally:
        await _cleanup(user_ids, instrument_ids)


def test_the_window_boundaries_are_the_ones_roll_risk_window_uses():
    """Pure, and total over a week -- including Monday, where the day and
    week boundaries coincide and the behavioural test above branches."""
    # Wednesday 2026-09-16 14:37 UTC.
    day, week = risk_window_starts(datetime(2026, 9, 16, 14, 37, 12, tzinfo=timezone.utc))
    assert day == datetime(2026, 9, 16, tzinfo=timezone.utc)
    assert week == datetime(2026, 9, 14, tzinfo=timezone.utc)

    for offset in range(7):
        now = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc) + timedelta(days=offset)
        day, week = risk_window_starts(now)
        assert day == datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
        assert week == datetime(2026, 9, 14, tzinfo=timezone.utc)
        # The same keys `_roll_risk_window` compares on.
        assert day.date() == now.date()
        assert week.isocalendar()[:2] == now.isocalendar()[:2]


# --- what the fix must not absorb, and must not break --------------------


async def test_paper_and_auto_trade_losses_stay_out_of_the_manual_counter(require_infra):
    """`execution_mode` cannot separate these: a manual stack with no
    connected broker trades against MockBroker and persists as PAPER,
    the same mode `/paper/*` and the auto-trade worker use. Without the
    journal-source filter, rehydration would make a restart *add* losses
    the running counter never had -- a different wrong answer, not a
    smaller one."""
    instrument_ids, _ = await _make_instruments(1)
    user_ids = []
    try:
        async with _client() as client:
            _, user_id = await _register(client)
            user_ids.append(user_id)

            now = datetime.now(timezone.utc)
            await _write_trade(user_id, instrument_ids[0], pnl=LOSING_TRADE_PNL, closed_at=now, source="manual_paper")
            await _write_trade(user_id, instrument_ids[0], pnl=LOSING_TRADE_PNL, closed_at=now, source="auto_trade")

            async with async_session_factory() as db:
                day_start, _ = risk_window_starts(now)
                assert await load_realized_pnl_since(db, user_id, ExecutionMode.PAPER, since=day_start) == 0.0
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_the_weekly_loss_counter_is_rehydrated_over_the_week_not_the_day(require_infra):
    """The weekly window, end to end on the stack rather than on the
    loader.

    Written because an injection escaped: measuring the weekly counter
    from the day boundary (`since=day_start` for both) left every test
    green, since nothing here read `stack.weekly_pnl` at all. A restart
    would then have quietly reset `max_weekly_loss_pct` to whatever today
    alone had lost.
    """
    instrument_ids, symbols = await _make_instruments(1)
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            now = datetime.now(timezone.utc)
            day_start, week_start = risk_window_starts(now)
            # Small enough that it cannot trip the daily gate even on a
            # Monday, where the two windows coincide.
            earlier_this_week = -50.0
            await _write_trade(user_id, instrument_ids[0], pnl=earlier_this_week, closed_at=week_start)

            _restart(user_id)
            assert (await client.post("/orders", json=_open(symbols[0]), headers=headers)).status_code == 201
            stack = _STACKS[user_id]

            assert stack.weekly_pnl == pytest.approx(earlier_this_week), "the week's loss was not rehydrated"
            if week_start == day_start:
                assert now.weekday() == 0  # Monday: the windows genuinely coincide.
                assert stack.daily_pnl == pytest.approx(earlier_this_week)
            else:
                assert stack.daily_pnl == 0.0, "a loss booked before today leaked into the daily counter"
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_live_stacks_counters_ignore_the_same_users_paper_rows(require_infra):
    """The `execution_mode` filter, which an injection also walked
    through: every account in this suite trades against MockBroker and
    persists as PAPER, so removing the filter changed nothing anywhere.

    It is load-bearing for a real account. The in-memory counter only ever
    counted what that stack itself placed, so rehydrating a LIVE stack
    from a PAPER row (or the reverse) would invent an order the running
    process never had. Asserted on the loaders, since making the stack
    resolve a real broker needs a connected broker account this
    environment has none of.
    """
    instrument_ids, symbols = await _make_instruments(1)
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)
            assert (await client.post("/orders", json=_open(symbols[0]), headers=headers)).status_code == 201

            now = datetime.now(timezone.utc)
            await _write_trade(
                user_id, instrument_ids[0], pnl=LOSING_TRADE_PNL, closed_at=now, execution_mode=ExecutionMode.LIVE
            )
            async with async_session_factory() as db:
                day_start, _ = risk_window_starts(now)
                assert await load_orders_placed_since(db, user_id, ExecutionMode.PAPER, since=day_start) == 1
                assert await load_orders_placed_since(db, user_id, ExecutionMode.LIVE, since=day_start) == 0
                assert await load_realized_pnl_since(db, user_id, ExecutionMode.PAPER, since=day_start) == 0.0
                assert await load_realized_pnl_since(
                    db, user_id, ExecutionMode.LIVE, since=day_start
                ) == pytest.approx(LOSING_TRADE_PNL)
    finally:
        await _cleanup(user_ids, instrument_ids)

async def test_one_users_trading_does_not_count_against_another(require_infra):
    """Control. The counters are per user, and a query missing its
    `user_id` filter would pass every test above."""
    instrument_ids, symbols = await _make_instruments(3)
    user_ids = []
    try:
        async with _client() as client:
            busy_headers, busy_user = await _register(client)
            quiet_headers, quiet_user = await _register(client)
            user_ids += [busy_user, quiet_user]

            # Closed one point down: a real loss, but nowhere near the 2%
            # daily limit, so what this measures is the per-user scoping
            # and not the loss gate tripping.
            assert (await client.post("/orders", json=_open(symbols[0]), headers=busy_headers)).status_code == 201
            assert (await client.post("/orders", json=_close(symbols[0], 99.0), headers=busy_headers)).status_code == 201
            busy_before = (_STACKS[busy_user].trades_today, _STACKS[busy_user].daily_pnl)

            _restart(busy_user)
            _restart(quiet_user)

            # Each account's first post-restart order rebuilds its own
            # stack, so this reads what each one actually rehydrated.
            assert (await client.post("/orders", json=_open(symbols[1]), headers=quiet_headers)).status_code == 201
            assert (await client.post("/orders", json=_open(symbols[2]), headers=busy_headers)).status_code == 201

            assert _STACKS[quiet_user].trades_today == 1, "the busy account's orders reached the quiet one"
            assert _STACKS[quiet_user].daily_pnl == 0.0, "the busy account's losses reached the quiet one"
            # The busy account rehydrated exactly what it had, plus the
            # one order that rebuilt its stack. Compared against what was
            # actually counted before the restart rather than a number
            # written here, so the assertion cannot drift from the
            # dedupe rules that decide when a counter moves at all.
            assert _STACKS[busy_user].trades_today == busy_before[0] + 1
            assert _STACKS[busy_user].daily_pnl == pytest.approx(busy_before[1])
            assert busy_before[1] < 0, "the busy account must actually have lost something"
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_rehydration_does_not_double_count_the_order_that_builds_the_stack(require_infra):
    """Control. The stack is built *during* the first order of the
    process, before that order has been journalled. A rehydration that ran
    later, or that counted the in-flight order as well as its row, would
    read 2 after one order and quietly halve every limit.

    The second half is the interaction with round 206's idempotency
    rehydration, which was measured rather than assumed: an *identical*
    resubmit after a restart is deduped, so it creates no order and
    increments nothing, and the counter stays at the journal's 1. The two
    rehydrations agree -- one order, counted once, whichever of them sees
    it."""
    instrument_ids, symbols = await _make_instruments(2)
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)

            assert (await client.post("/orders", json=_open(symbols[0]), headers=headers)).status_code == 201
            assert _STACKS[user_id].trades_today == 1

            _restart(user_id)
            assert (await client.post("/orders", json=_open(symbols[1]), headers=headers)).status_code == 201
            assert _STACKS[user_id].trades_today == 2, f"counted {_STACKS[user_id].trades_today}"

            _restart(user_id)
            assert (await client.post("/orders", json=_open(symbols[1]), headers=headers)).status_code == 201
            assert _STACKS[user_id].trades_today == 2, (
                f"a deduped resubmit must not spend another order: counted {_STACKS[user_id].trades_today}"
            )
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_the_rehydrated_counters_still_reset_at_the_next_day_boundary(require_infra):
    """`_roll_risk_window` only resets when the day key CHANGES, so a
    build that rehydrated the counters but left `_risk_day` as None would
    have the next day's first order adopt that day's key without ever
    resetting -- carrying a spent allowance into a day it does not belong
    to. Round 72's fix and this one have to hold at the same time."""
    instrument_ids, symbols = await _make_instruments(1)
    user_ids = []
    try:
        async with _client() as client:
            headers, user_id = await _register(client)
            user_ids.append(user_id)
            assert (await client.post("/orders", json=_open(symbols[0]), headers=headers)).status_code == 201
            assert (await client.post("/orders", json=_close(symbols[0], 75.0), headers=headers)).status_code == 201

            _restart(user_id)
            # Rebuild the stack through a real request, then roll it.
            await client.get("/positions", headers=headers)
            stack = _STACKS.get(user_id)
            if stack is None:  # /positions does not build a stack on its own
                assert (await client.post("/orders", json=_open(symbols[0]), headers=headers)).status_code == 201
                stack = _STACKS[user_id]

            assert stack._risk_day is not None, "a rehydrated stack must know which day it rehydrated"
            assert stack.daily_pnl < 0

            stack._roll_risk_window(datetime.now(timezone.utc) + timedelta(days=1))
            assert stack.trades_today == 0
            assert stack.daily_pnl == 0.0
    finally:
        await _cleanup(user_ids, instrument_ids)
