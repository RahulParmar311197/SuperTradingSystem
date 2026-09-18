"""A halted account stopped enforcing the stop on a position already open.

`AutoTradeSupervisor.run_once` `continue`d past any halted user. That is
right for entries and wrong for exits, because **there is no broker-side
protective order behind an auto-traded position**: `ensure_protective_stop`
is called only from `POST /orders`, so `PaperTradingEngine._maybe_exit`,
run on the candles this loop feeds it, *is* the stop.

Measured on the stop-loss fixture in `test_auto_trade_worker.py`, halting
the account after the entry filled and before the bar that breaks the stop:

    not halted -> 1 trade, no open position
    halted     -> 0 trades, position still open, stop 99.70,
                  on a bar whose low was 90

The ruling this now follows is already made twice in this codebase, in its
own words: reconciliation halts an account precisely when its positions
look wrong, which is "the worst moment to forbid closing them". `POST
/orders` and `POST /options/execute` both exempt a reducing order from the
halt for that reason. This path had the exemption missing rather than
declined.

Three other gates leave the same position unmanaged and are **not** changed
here — `auto_trading_enabled` turned off, the AUTO_TRADE permission
revoked, the strategy deactivated. Whether the loop should keep honouring a
stop it placed after the operator switched the robot off has two defensible
answers (`POST /orders` requires the LIVE_TRADE permission for every order
including a reducing one, which argues for stopping; a stop that silently
stops existing argues for continuing), and guessing it is not this round's
to do. What is not in question is that it must not be silent, so those now
log an error and fire a notification.
"""

import logging
import uuid
from datetime import timedelta

from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.core.redis import halt_account, resume_account
from app.database.models.market import Candle as CandleRow
from app.database.models.notifications import Notification, NotificationType
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import Position, Trade as TradeRow
from app.database.models.users import TradingPermission, User
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.smc.types import Candle
from app.workers.auto_trade_worker import AutoTradeSupervisor
from tests.workers.test_auto_trade_worker import (
    LIQUID_BAR_VOLUME,
    STRATEGY_DEFINITION,
    _cleanup,
    _recent_start,
)

# The bullish FVG-retest setup, then a bar that reverses hard through the
# stop. Index 8 fills the entry; index 9 trades to 90 against a stop of
# 99.70, so whether the stop is enforced is the whole difference between
# the two outcomes below.
STOP_LOSS_SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),  # entry fills here
    (104, 105, 90, 92),    # breaks the stop
]


async def _make_user(auto_enabled: bool = True) -> tuple[uuid.UUID, uuid.UUID]:
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"exitmgmt-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Exit Management",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=auto_enabled,
            auto_trading_risk_per_trade_pct=1.0,
        )
        db.add(user)
        await db.flush()
        strategy = StrategyRow(
            user_id=user.id,
            name="Bullish FVG retest",
            definition=dict(STRATEGY_DEFINITION),
            is_active=True,
            eligible_for_auto_trading=True,
        )
        db.add(strategy)
        await db.commit()
        return user.id, strategy.id


async def _drive(instrument, user_id, *, disrupt=None, supervisor=None) -> AutoTradeSupervisor:
    """Feeds the setup one bar at a time, applying `disrupt` on the first
    pass that leaves a position open. Returns the supervisor so a caller can
    keep driving it."""
    async with async_session_factory() as db:
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument.id))
        await db.commit()

    start = _recent_start()
    candles = [
        Candle(start + timedelta(minutes=i), o, h, low, c, LIQUID_BAR_VOLUME)
        for i, (o, h, low, c) in enumerate(STOP_LOSS_SETUP)
    ]
    supervisor = supervisor or AutoTradeSupervisor(timeframe="15m")
    disrupted = False
    for i, candle in enumerate(candles):
        async with async_session_factory() as db:
            await upsert_candles(db, instrument.id, "15m", [candle])
        await supervisor.run_once()
        if disrupt is not None and not disrupted and i < len(candles) - 1:
            async with async_session_factory() as db:
                open_now = (
                    await db.execute(
                        select(Position).where(Position.user_id == user_id, Position.is_open.is_(True))
                    )
                ).scalars().all()
            if open_now:
                await disrupt(user_id)
                disrupted = True
    assert disrupt is None or disrupted, "the fixture must reach an open position before disrupting"
    return supervisor


async def _state(user_id) -> tuple[int, int, set[str]]:
    async with async_session_factory() as db:
        trades = (await db.execute(select(TradeRow).where(TradeRow.user_id == user_id))).scalars().all()
        still_open = (
            await db.execute(select(Position).where(Position.user_id == user_id, Position.is_open.is_(True)))
        ).scalars().all()
        notes = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
    return len(trades), len(still_open), {n.type.value for n in notes}


# --- the finding ---------------------------------------------------------


async def test_a_halted_account_still_takes_the_stop(db_instrument):
    """Behavioural proof. Before this, a halt left the position open through
    a bar that traded 9.7 points below its stop."""
    user_id, _ = await _make_user()
    strategy_market = db_instrument.symbol
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.definition = {**STRATEGY_DEFINITION, "market": strategy_market}
        await db.commit()
    try:
        await _drive(
            db_instrument,
            user_id,
            disrupt=lambda uid: halt_account(str(uid), "reconciliation found a mismatch"),
        )
        trades, still_open, notes = await _state(user_id)
        assert trades == 1, "the stop must still have closed the position"
        assert still_open == 0
        assert NotificationType.SL_HIT.value in notes, notes
    finally:
        await resume_account(str(user_id))
        await _cleanup(user_id)


async def test_a_halt_still_blocks_a_new_entry(db_instrument):
    """Control, and the property the original `continue` existed for. The
    exemption must let an exit through and nothing else — a version that
    simply stopped checking the halt would satisfy the proof above."""
    user_id, _ = await _make_user()
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.definition = {**STRATEGY_DEFINITION, "market": db_instrument.symbol}
        await db.commit()
    try:
        await halt_account(str(user_id), "halted before anything opened")
        await _drive(db_instrument, user_id)
        async with async_session_factory() as db:
            positions = (
                await db.execute(select(Position).where(Position.user_id == user_id))
            ).scalars().all()
        trades, _, _ = await _state(user_id)
        assert positions == [], "a halted account must open nothing"
        assert trades == 0
    finally:
        await resume_account(str(user_id))
        await _cleanup(user_id)


# --- the gates this round deliberately does not change -------------------


async def _disable_auto_trading(user_id) -> None:
    async with async_session_factory() as db:
        user = await db.get(User, user_id)
        user.auto_trading_enabled = False
        await db.commit()


async def _deactivate_strategy(user_id) -> None:
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.is_active = False
        await db.commit()


async def test_an_unmanaged_position_is_reported(db_instrument, caplog):
    """Behavioural proof. Turning auto-trading off still abandons the stop —
    that is the open modelling question — but it may not do so silently."""
    user_id, _ = await _make_user()
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.definition = {**STRATEGY_DEFINITION, "market": db_instrument.symbol}
        await db.commit()
    try:
        with caplog.at_level(logging.ERROR, logger="workers.autotrade"):
            await _drive(db_instrument, user_id, disrupt=_disable_auto_trading)

        trades, still_open, notes = await _state(user_id)
        assert (trades, still_open) == (0, 1), "the position is genuinely left open — the point of the report"
        assert NotificationType.RECONCILIATION_REQUIRED.value in notes, notes
        messages = [r.getMessage() for r in caplog.records if "no longer being managed" in r.getMessage()]
        assert messages, [r.getMessage() for r in caplog.records]
        assert "stop" in messages[0].lower(), messages[0]
    finally:
        await _cleanup(user_id)


async def test_a_deactivated_strategy_is_reported_too(db_instrument):
    """Behavioural proof at a second gate. The sweep reads the `positions`
    table rather than the strategy list, so it must not depend on *which*
    gate stopped the loop."""
    user_id, _ = await _make_user()
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.definition = {**STRATEGY_DEFINITION, "market": db_instrument.symbol}
        await db.commit()
    try:
        await _drive(db_instrument, user_id, disrupt=_deactivate_strategy)
        trades, still_open, notes = await _state(user_id)
        assert (trades, still_open) == (0, 1)
        assert NotificationType.RECONCILIATION_REQUIRED.value in notes, notes
    finally:
        await _cleanup(user_id)


async def test_the_report_is_once_per_position_not_once_per_pass(db_instrument):
    """Behavioural proof. This loop runs every 60 seconds; an operator who
    has to filter the warning will not read it."""
    user_id, _ = await _make_user()
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.definition = {**STRATEGY_DEFINITION, "market": db_instrument.symbol}
        await db.commit()
    try:
        supervisor = await _drive(db_instrument, user_id, disrupt=_disable_auto_trading)
        for _ in range(4):
            await supervisor.run_once()

        async with async_session_factory() as db:
            notes = (
                await db.execute(
                    select(Notification).where(
                        Notification.user_id == user_id,
                        Notification.type == NotificationType.RECONCILIATION_REQUIRED,
                    )
                )
            ).scalars().all()
        assert len(notes) == 1, [n.title for n in notes]
    finally:
        await _cleanup(user_id)


# --- and what must stay quiet --------------------------------------------


async def test_an_ordinary_round_trip_reports_nothing(db_instrument):
    """Control, and the one that caught a defect in this very fix.

    The managed-key was first recorded where the *already open* position is
    read — but the pass that opens a position sees none beforehand, so every
    fresh entry reported itself as unmanaged on its own pass. Measured: one
    spurious notification per trade, which is precisely how a real warning
    becomes noise."""
    user_id, _ = await _make_user()
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.definition = {**STRATEGY_DEFINITION, "market": db_instrument.symbol}
        await db.commit()
    try:
        await _drive(db_instrument, user_id)
        trades, still_open, notes = await _state(user_id)
        assert (trades, still_open) == (1, 0)
        assert NotificationType.RECONCILIATION_REQUIRED.value not in notes, notes
    finally:
        await _cleanup(user_id)


async def test_a_halted_account_with_nothing_open_does_nothing(db_instrument):
    """Control. The exemption must not turn a halted, flat account into a
    source of reports or orders — there is nothing to manage."""
    user_id, _ = await _make_user()
    async with async_session_factory() as db:
        row = (await db.execute(select(StrategyRow).where(StrategyRow.user_id == user_id))).scalars().one()
        row.definition = {**STRATEGY_DEFINITION, "market": db_instrument.symbol}
        await db.commit()
    try:
        await halt_account(str(user_id), "halted and flat")
        await _drive(db_instrument, user_id)
        trades, still_open, notes = await _state(user_id)
        assert (trades, still_open) == (0, 0)
        assert notes == set(), notes
    finally:
        await resume_account(str(user_id))
        await _cleanup(user_id)
