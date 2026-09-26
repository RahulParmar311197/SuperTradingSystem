"""The auto-trade worker's account had no cash of its own, and every
engine it built had a different one.

Round 170 made `MockBroker`'s balance follow realized P&L. On this path
that exposed two things:

1. The same restart gap rounds 154/155 closed for `trades_today`,
   `daily_pnl` and `weekly_pnl`, now applying to the DENOMINATOR those
   three are measured against. `_balance` lives in the worker process, so
   every deploy, OOM kill or supervision restart handed the account back
   its starting balance while the losses came back from Postgres.

2. `PaperTradingEngine` builds its own `MockBroker` when none is passed,
   and this supervisor runs one engine per (user, strategy, instrument).
   Cash is an account-level quantity, so N engines meant one account with
   N independent balances -- harmless only while that balance never
   moved, which is exactly what this round changed. It is the same
   defect, and the same shape of fix, as the per-user `_position_managers`
   and `_risk_windows` registries beside it.
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
from app.trading.persistence import AUTO_TRADE_SOURCE
from app.workers.auto_trade_worker import AutoTradeSupervisor

# The same bullish sweep+FVG dataset the sibling worker tests use: it
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

# The equity liquidity gate caps an order at a share of the bar's traded
# volume, and these setups size to hundreds of shares.
LIQUID_BAR_VOLUME = 50_000.0
BALANCE = 100_000.0


def _recent_start() -> datetime:
    """Anchored recently so the supervisor's freshness gate does not
    refuse the series."""
    return datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=30)


def _candles() -> list[Candle]:
    start = _recent_start()
    return [Candle(start + timedelta(minutes=i), o, h, l, c, LIQUID_BAR_VOLUME) for i, (o, h, l, c) in enumerate(SETUP)]


async def _instrument() -> Instrument:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"ABR{uuid.uuid4().hex[:7].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument


async def _auto_trading_user(*, markets: list[str]) -> uuid.UUID:
    """One strategy per market, so the supervisor builds one engine per
    instrument for this user -- which is the condition test 2 needs."""
    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"abr-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Auto Balance",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
            auto_trading_risk_per_trade_pct=1.0,
            auto_trading_max_positions=5,
            auto_trading_max_trades_per_day=10,
            auto_trading_daily_loss_limit_pct=100.0,
        )
        db.add(user)
        await db.flush()
        for market in markets:
            db.add(
                StrategyRow(
                    user_id=user.id,
                    name=f"Bullish FVG retest {market}",
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


async def _write_auto_trade(user_id: uuid.UUID, instrument_id: uuid.UUID, *, pnl: float, when: datetime) -> None:
    async with async_session_factory() as db:
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
                journal={"source": AUTO_TRADE_SOURCE, "strategy": "Bullish FVG retest", "symbol": "X"},
            )
        )
        await db.commit()


async def _balance(supervisor: AutoTradeSupervisor, user_id: uuid.UUID) -> float:
    return (await supervisor._brokers[str(user_id)].get_account()).balance


# --- the findings ---------------------------------------------------------


async def test_a_worker_restart_rebuilds_the_accounts_balance_from_the_journal(require_infra):
    """The headline. A month of auto-traded losses totalling -40,000, and
    a supervisor that has never seen them: it must still start from
    60,000, not from the starting balance."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(markets=[instrument.symbol])
        user_ids.append(user_id)
        await _write_auto_trade(
            user_id, instrument.id, pnl=-40_000.0, when=datetime.now(timezone.utc) - timedelta(days=30)
        )

        # The restart: a supervisor with the same database and no memory.
        restarted = AutoTradeSupervisor(timeframe="15m")
        await _feed(restarted, instrument.id, _candles(), bars=8)

        assert await _balance(restarted, user_id) == pytest.approx(BALANCE - 40_000.0)
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_a_fresh_account_still_starts_at_the_starting_balance(require_infra):
    """Non-vacuity control for the test above: with nothing in the
    journal the rebuild is a no-op, so that assertion is about the 40,000
    and not about the balance being wrong in general."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        user_id = await _auto_trading_user(markets=[instrument.symbol])
        user_ids.append(user_id)

        supervisor = AutoTradeSupervisor(timeframe="15m")
        await _feed(supervisor, instrument.id, _candles(), bars=8)

        assert await _balance(supervisor, user_id) == pytest.approx(BALANCE)
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_two_instruments_for_one_user_trade_against_one_balance(require_infra):
    """The second defect. `PaperTradingEngine` builds its own broker when
    none is passed, so before the per-user registry these two engines held
    two independent 100,000 accounts -- and a loss booked on one was
    invisible to the other's risk budget forever."""
    a, b = await _instrument(), await _instrument()
    user_ids, instrument_ids = [], [a.id, b.id]
    try:
        user_id = await _auto_trading_user(markets=[a.symbol, b.symbol])
        user_ids.append(user_id)

        supervisor = AutoTradeSupervisor(timeframe="15m")
        await _feed(supervisor, a.id, _candles(), bars=8)
        await _feed(supervisor, b.id, _candles(), bars=8)

        # `_engines` is keyed (user_id, strategy_id, instrument_id) and the
        # supervisor pairs every eligible strategy with every instrument in
        # the database, so this user has hundreds of engines in the shared
        # test database. Scope to the two instruments this test created --
        # 2 strategies x 2 instruments = 4 -- which is also the point: the
        # per-account registry is what stops one account owning hundreds of
        # independent 100,000 balances.
        mine = {str(a.id), str(b.id)}
        engines = [e for key, e in supervisor._engines.items() if key[0] == str(user_id) and key[2] in mine]
        assert len(engines) == 4, f"expected 2 strategies x 2 instruments, got {len(engines)}"

        brokers = {id(e.broker) for e in engines}
        assert len(brokers) == 1, (
            f"one account, {len(brokers)} brokers: each engine has its own cash"
        )
        assert engines[0].broker is supervisor._brokers[str(user_id)]
    finally:
        await _cleanup(user_ids, instrument_ids)


async def test_one_users_losses_do_not_move_anothers_balance(require_infra):
    """Scoping control, in both directions: the registry is keyed per
    user, and the journal read is filtered by user."""
    instrument = await _instrument()
    user_ids, instrument_ids = [], [instrument.id]
    try:
        poor = await _auto_trading_user(markets=[instrument.symbol])
        rich = await _auto_trading_user(markets=[instrument.symbol])
        user_ids += [poor, rich]
        await _write_auto_trade(
            poor, instrument.id, pnl=-40_000.0, when=datetime.now(timezone.utc) - timedelta(days=30)
        )

        supervisor = AutoTradeSupervisor(timeframe="15m")
        await _feed(supervisor, instrument.id, _candles(), bars=8)

        assert await _balance(supervisor, poor) == pytest.approx(BALANCE - 40_000.0)
        assert await _balance(supervisor, rich) == pytest.approx(BALANCE)
    finally:
        await _cleanup(user_ids, instrument_ids)
