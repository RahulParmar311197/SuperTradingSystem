"""A worker restart cleared the auto-trading day's risk counters.

Round 154 fixed this for the manual `/orders` stack. `AutoTradeSupervisor`
has the same shape and the same gap: it rebuilds its `PositionManager`
from Postgres on first use -- the comment at that very site says "same
restart gap the manual stack had" -- and then builds a zeroed `RiskWindow`
one line below it.

Measured, one strategy on two instruments, `auto_trading_max_trades_per_day=1`:

    instrument A, supervisor 1 -> 1 position, trades_today=1
    worker restart
    instrument B, supervisor 2 -> 2 positions against a cap of 1,
                                  0 rejections naming max_trades_per_day

Every worker start handed the account a fresh `max_trades_per_day`,
`max_daily_loss_pct` and `max_weekly_loss_pct` -- on the path nobody is
watching, and the worker restarts on every deploy and whenever its
supervision loop restarts it.

Rebuilding `trades_today` here is not the manual path's single row count.
That counter moves when an entry *opens*, and this path journals a `trades`
row only when a position *closes*, so an entry taken today and still
running has no trade row at all. It is two disjoint sets: trades opened
today, plus positions opened today that are still open.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog, RiskEvent
from app.database.models.strategy import Direction
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import ExecutionMode, Position, Trade as TradeRow
from app.database.models.users import TradingPermission, User
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.smc.types import Candle
from app.trading.persistence import (
    AUTO_TRADE_SOURCE,
    load_auto_trade_entries_since,
    load_auto_trade_realized_pnl_since,
    risk_window_starts,
)
from app.workers.auto_trade_worker import AUTO_SOURCE_KEY, AutoTradeSupervisor

# The same bullish sweep+FVG dataset every other auto-trade test uses: it
# matches on bar 8 and runs to target on bar 9.
SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces into the FVG -> entry
    (104, 130, 104, 128),  # runs to target -> close
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

# See the sibling file's note: the equity liquidity gate caps an order at a
# share of the bar's traded volume, and these setups size to hundreds of
# shares.
LIQUID_BAR_VOLUME = 50_000.0

# MockBroker's starting balance, what the loss percentages measure against.
BALANCE = 100_000.0


def _recent_start() -> datetime:
    """Anchored recently so the supervisor's freshness gate does not refuse
    the series. Same reasoning as the sibling worker tests."""
    return datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)


def _candles() -> list[Candle]:
    start = _recent_start()
    return [Candle(start + timedelta(minutes=i), o, h, l, c, LIQUID_BAR_VOLUME) for i, (o, h, l, c) in enumerate(SETUP)]


async def _instrument() -> Instrument:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"WRK{uuid.uuid4().hex[:7].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument


async def _auto_trading_user(*, market: str, max_trades_per_day: int = 1, daily_loss_pct: float = 2.0) -> uuid.UUID:
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"wrk-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Worker Restart",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
            auto_trading_risk_per_trade_pct=1.0,
            auto_trading_max_positions=5,
            auto_trading_max_trades_per_day=max_trades_per_day,
            auto_trading_daily_loss_limit_pct=daily_loss_pct,
        )
        db.add(user)
        await db.flush()
        db.add(
            StrategyRow(
                user_id=user.id,
                name="Bullish FVG retest",
                definition={**STRATEGY_DEFINITION, "market": market},
                is_active=True,
                eligible_for_auto_trading=True,
            )
        )
        await db.commit()
        return user.id


async def _cleanup(user_ids: list[uuid.UUID], instrument_ids: list[uuid.UUID]) -> None:
    """Child rows first: `trades` references `positions`."""
    async with async_session_factory() as db:
        for user_id in user_ids:
            for model in (TradeRow, Position, Notification, AuditLog, RiskEvent, StrategyRow):
                await db.execute(delete(model).where(model.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        for instrument_id in instrument_ids:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def _feed(supervisor: AutoTradeSupervisor, instrument_id: uuid.UUID, candles: list[Candle], bars: int) -> None:
    for i in range(bars):
        async with async_session_factory() as db:
            await upsert_candles(db, instrument_id, "15m", [candles[i]])
        await supervisor.run_once()


async def _positions(user_id: uuid.UUID) -> list[Position]:
    async with async_session_factory() as db:
        return list((await db.execute(select(Position).where(Position.user_id == user_id))).scalars().all())


async def _rejections_naming(user_id: uuid.UUID, check: str) -> list[RiskEvent]:
    async with async_session_factory() as db:
        events = (await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))).scalars().all()
    return [e for e in events if e.checks is not None and e.checks.get(check) is False]


async def _write_auto_trade(
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    *,
    pnl: float,
    when: datetime,
    source: str | None = AUTO_TRADE_SOURCE,
    strategy_id: uuid.UUID | None = None,
) -> None:
    """An auto-trade journal row at a chosen time. `source=None` writes a
    row in the pre-`AUTO_TRADE_SOURCE` shape."""
    journal = {"strategy": "Bullish FVG retest", "symbol": "X"}
    if source is not None:
        journal["source"] = source
    async with async_session_factory() as db:
        if strategy_id is None:
            strategy_id = (
                await db.execute(select(StrategyRow.id).where(StrategyRow.user_id == user_id))
            ).scalars().first()
        db.add(
            TradeRow(
                user_id=user_id,
                instrument_id=instrument_id,
                strategy_id=strategy_id,
                execution_mode=ExecutionMode.PAPER,
                direction=Direction.LONG,
                entry_price=100.0,
                exit_price=90.0,
                quantity=10.0,
                pnl=pnl,
                opened_at=when,
                closed_at=when,
                journal=journal,
            )
        )
        await db.commit()


# --- the finding ----------------------------------------------------------


async def test_a_worker_restart_does_not_hand_the_account_a_fresh_daily_cap(require_infra):
    """Behavioural proof. The second supervisor is what a restart
    produces: same database, no memory of the first."""
    a, b = await _instrument(), await _instrument()
    user_ids, instrument_ids = [], [a.id, b.id]
    try:
        user_id = await _auto_trading_user(market=a.symbol)
        user_ids.append(user_id)
        candles = _candles()

        await _feed(AutoTradeSupervisor(timeframe="15m"), a.id, candles, bars=9)
        assert len(await _positions(user_id)) == 1

        restarted = AutoTradeSupervisor(timeframe="15m")
        await _feed(restarted, b.id, candles, bars=9)

        positions = await _positions(user_id)
        assert len(positions) == 1, f"a worker restart lifted the daily cap: {len(positions)} positions against a cap of 1"
        assert restarted._risk_windows[str(user_id)].trades_today == 1
        assert await _rejections_naming(user_id, "max_trades_per_day"), (
            "the second instrument must have been stopped by the cap itself, not by some other check"
        )
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_worker_restart_does_not_clear_the_daily_loss(require_infra):
    """The gate that matters most on this path: an account that has
    already lost past its daily limit must not start trading again because
    the worker bounced."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        # Cap of 10 so trades-per-day cannot be what stops it; the loss
        # limit has to be the binding constraint.
        user_id = await _auto_trading_user(market=instrument.symbol, max_trades_per_day=10, daily_loss_pct=2.0)
        user_ids.append(user_id)
        # 2.5% of the account, past the 2% limit. Written to the journal
        # rather than traded, because the shared fixture is a winner and
        # what is under test is what a *restart* reads back, not how the
        # loss was booked.
        await _write_auto_trade(
            user_id, instrument.id, pnl=-(BALANCE * 0.025), when=datetime.now(timezone.utc)
        )

        await _feed(AutoTradeSupervisor(timeframe="15m"), instrument.id, _candles(), bars=9)

        assert await _positions(user_id) == [], "a restarted worker traded past the daily loss limit"
        assert await _rejections_naming(user_id, "daily_loss_limit"), "expected a rejection naming daily_loss_limit"
    finally:
        await _cleanup(user_ids, instrument_ids)


# --- the two halves of the entry count ------------------------------------


async def test_an_entry_still_open_counts_even_though_it_has_no_trade_row(require_infra):
    """The half that makes this different from the manual path. A trade
    row appears only on close, so counting trades alone would read zero
    while a position opened minutes ago sits open -- and the cap would be
    spendable twice over."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)

        await _feed(AutoTradeSupervisor(timeframe="15m"), instrument.id, _candles(), bars=9)
        open_positions = [p for p in await _positions(user_id) if p.is_open]
        assert len(open_positions) == 1

        async with async_session_factory() as db:
            trades = (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
            assert trades == [], "this test is only meaningful while the entry is still open"
            day_start, _ = risk_window_starts(datetime.now(timezone.utc))
            counted = await load_auto_trade_entries_since(
                db, user_id, since=day_start, source_key=AUTO_SOURCE_KEY
            )
        assert counted == 1, f"an open entry with no trade row counted {counted}"
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_round_trip_counts_once_not_twice(require_infra):
    """The disjointness claim. Closing flips `is_open` on the same row
    rather than deleting it, so a naive "trades + all positions" would
    count a completed round trip twice and halve the cap."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)

        # All ten bars: bar 8 opens, bar 9 runs to target and closes.
        await _feed(AutoTradeSupervisor(timeframe="15m"), instrument.id, _candles(), bars=10)

        async with async_session_factory() as db:
            trades = (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
            rows = (await db.execute(select(Position).where(Position.user_id == user_id))).scalars().all()
            assert len(trades) == 1, "this test needs the round trip to have completed"
            assert [r.is_open for r in rows] == [False], "the closed position's row must still be there"
            day_start, _ = risk_window_starts(datetime.now(timezone.utc))
            counted = await load_auto_trade_entries_since(
                db, user_id, since=day_start, source_key=AUTO_SOURCE_KEY
            )
        assert counted == 1, f"a single round trip counted {counted}"
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_the_weekly_counter_is_rebuilt_over_the_week_not_the_day(require_infra):
    """The weekly window on the window itself, not on the loader beneath
    it. Written because an injection escaped: measuring the weekly counter
    from the day boundary left every test green, since nothing read
    `RiskWindow.weekly_pnl` at all."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)
        now = datetime.now(timezone.utc)
        day_start, week_start = risk_window_starts(now)
        # Small enough not to trip the daily gate even on a Monday, where
        # the two windows coincide.
        earlier_this_week = -40.0
        await _write_auto_trade(user_id, instrument.id, pnl=earlier_this_week, when=week_start)

        supervisor = AutoTradeSupervisor(timeframe="15m")
        await _feed(supervisor, instrument.id, _candles(), bars=9)
        window = supervisor._risk_windows[str(user_id)]

        assert window.weekly_pnl == pytest.approx(earlier_this_week), "the week's loss was not rebuilt"
        if week_start == day_start:
            assert now.weekday() == 0  # Monday: the windows genuinely coincide.
            assert window.daily_pnl == pytest.approx(earlier_this_week)
        else:
            assert window.daily_pnl == 0.0, "a loss booked before today leaked into the daily counter"
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_manual_position_does_not_spend_the_auto_trading_allowance(require_infra):
    """The `source_key` filter on the open-position half, which an
    injection also walked through: nothing here held a manual position for
    the same account.

    A user who both auto-trades and places orders by hand has rows of both
    kinds in `positions`. Counting the manual one would spend the
    auto-trading cap on a trade the worker never took -- and the manual
    stack has already counted it against its own."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)

        async with async_session_factory() as db:
            db.add(
                Position(
                    user_id=user_id,
                    instrument_id=instrument.id,
                    execution_mode=ExecutionMode.PAPER,
                    quantity=10.0,
                    average_price=100.0,
                    is_open=True,
                    source_key="manual",
                )
            )
            await db.commit()

        async with async_session_factory() as db:
            day_start, _ = risk_window_starts(datetime.now(timezone.utc))
            counted = await load_auto_trade_entries_since(
                db, user_id, since=day_start, source_key=AUTO_SOURCE_KEY
            )
        assert counted == 0, f"a manual position spent {counted} of the auto-trading allowance"
    finally:
        await _cleanup(user_ids, instrument_ids)

# --- windows, and what must stay out --------------------------------------


async def test_yesterdays_auto_trading_does_not_count_today(require_infra):
    """The over-fix: summing the whole journal, which would make a daily
    limit cumulative-forever -- the exact bug round 72 fixed from the
    other side."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)
        yesterday = datetime.now(timezone.utc) - timedelta(days=1)
        await _write_auto_trade(user_id, instrument.id, pnl=-500.0, when=yesterday)

        async with async_session_factory() as db:
            day_start, _ = risk_window_starts(datetime.now(timezone.utc))
            assert await load_auto_trade_entries_since(
                db, user_id, since=day_start, source_key=AUTO_SOURCE_KEY
            ) == 0
            assert await load_auto_trade_realized_pnl_since(db, user_id, since=day_start) == 0.0
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_loss_earlier_this_week_counts_weekly_but_not_daily(require_infra):
    """Day and week are different windows; measuring both from the day
    boundary would leave `max_weekly_loss_pct` seeing only today."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)
        now = datetime.now(timezone.utc)
        day_start, week_start = risk_window_starts(now)
        await _write_auto_trade(user_id, instrument.id, pnl=-250.0, when=week_start)

        async with async_session_factory() as db:
            weekly = await load_auto_trade_realized_pnl_since(db, user_id, since=week_start)
            daily = await load_auto_trade_realized_pnl_since(db, user_id, since=day_start)

        assert weekly == pytest.approx(-250.0)
        if week_start == day_start:
            # Monday: the week starts today, so the windows coincide.
            # Asserted rather than skipped, so this proves something every
            # day of the week.
            assert now.weekday() == 0
            assert daily == pytest.approx(-250.0)
        else:
            assert daily == 0.0
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_manual_and_paper_trades_stay_out_of_the_auto_counter(require_infra):
    """The mirror of round 154's control. All three writers use
    `ExecutionMode.PAPER`, so only `journal.source` separates them; without
    that filter a restart would charge the auto-trading limits with losses
    the worker never took."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)
        now = datetime.now(timezone.utc)
        await _write_auto_trade(user_id, instrument.id, pnl=-1000.0, when=now, source="manual_order")
        await _write_auto_trade(user_id, instrument.id, pnl=-1000.0, when=now, source="manual_paper")

        async with async_session_factory() as db:
            day_start, _ = risk_window_starts(now)
            assert await load_auto_trade_realized_pnl_since(db, user_id, since=day_start) == 0.0
            assert await load_auto_trade_entries_since(
                db, user_id, since=day_start, source_key=AUTO_SOURCE_KEY
            ) == 0
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_rows_written_before_the_source_key_existed_still_count(require_infra):
    """The compatibility clause, which is not decoration: leaving these
    out would under-count on the day a deployment lands, and an unseen
    loss is a loss the limit does not know about."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)
        now = datetime.now(timezone.utc)
        await _write_auto_trade(user_id, instrument.id, pnl=-750.0, when=now, source=None)

        async with async_session_factory() as db:
            day_start, _ = risk_window_starts(now)
            assert await load_auto_trade_realized_pnl_since(db, user_id, since=day_start) == pytest.approx(-750.0)
            assert await load_auto_trade_entries_since(
                db, user_id, since=day_start, source_key=AUTO_SOURCE_KEY
            ) == 1
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_one_accounts_auto_trading_does_not_count_against_another(require_infra):
    """Control. A query missing its `user_id` filter would satisfy every
    test above."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        busy = await _auto_trading_user(market=instrument.symbol)
        quiet = await _auto_trading_user(market=instrument.symbol)
        user_ids += [busy, quiet]
        now = datetime.now(timezone.utc)
        await _write_auto_trade(busy, instrument.id, pnl=-900.0, when=now)

        async with async_session_factory() as db:
            day_start, _ = risk_window_starts(now)
            assert await load_auto_trade_realized_pnl_since(db, quiet, since=day_start) == 0.0
            assert await load_auto_trade_entries_since(
                db, quiet, since=day_start, source_key=AUTO_SOURCE_KEY
            ) == 0
            assert await load_auto_trade_realized_pnl_since(db, busy, since=day_start) == pytest.approx(-900.0)
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_the_rehydrated_window_still_rolls_at_the_next_day(require_infra):
    """`RiskWindow.roll` only resets when the day mark CHANGES, so a
    rebuild that left `risk_day` as None would let the next day's first
    candle adopt that day's mark without resetting -- carrying a spent
    allowance into it. Round 72's fix and this one have to hold at once."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)

        supervisor = AutoTradeSupervisor(timeframe="15m")
        await _feed(supervisor, instrument.id, _candles(), bars=9)
        window = supervisor._risk_windows[str(user_id)]

        assert window.risk_day is not None, "a rebuilt window must know which day it covers"
        assert window.trades_today == 1

        window.roll(datetime.now(timezone.utc) + timedelta(days=1))
        assert window.trades_today == 0
        assert window.daily_pnl == 0.0
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_repeated_rejections_is_deliberately_not_rebuilt(require_infra):
    """A recorded decision, not an oversight.

    `repeated_rejections` counts *consecutive* broker rejections. This path
    writes no `orders` rows at all and nothing durable records a rejection
    as one, so there is no journal to rebuild it from -- and a consecutive
    counter cannot be inferred from a daily total. Zero is what a fresh
    process legitimately knows. This test exists so that stays a decision
    someone made rather than something nobody noticed.
    """
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(market=instrument.symbol)
        user_ids.append(user_id)

        supervisor = AutoTradeSupervisor(timeframe="15m")
        await _feed(supervisor, instrument.id, _candles(), bars=9)
        assert supervisor._risk_windows[str(user_id)].repeated_rejections == 0
    finally:
        await _cleanup(user_ids, instrument_ids)
