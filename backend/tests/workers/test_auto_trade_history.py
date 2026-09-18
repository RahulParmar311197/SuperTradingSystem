"""`AutoTradeSupervisor` analyses the instrument's stored history, not its own uptime.

`PaperTradingEngine` keeps its own `self.candles`, starts it empty, appends
one bar per `on_candle`, and runs `smc_engine.analyze(self.candles)` over
that list. The supervisor already loads the whole stored series and then
passed only `candles[-1]`, so the analysis window was however long the
worker process had been running -- while every sibling (`ScannerWorker`,
`POST /scanner`, `GET /charts/{id}/smc`, `POST /backtest`, `POST /replay`,
`POST /ai/analyze`) passes the full series.

Every test in `test_auto_trade_worker.py` inserts one candle then calls
`run_once()`, in a loop, so the DB history and the engine's in-memory
history are identical by construction and the divergence cannot appear.
Its `SETUP` fixture is also ten bars one minute apart inside a single
morning, so `detect_session_levels` returns `[]` for both windows and the
previous-day/week path is never exercised at all.
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
from app.database.models.strategy import Setup as SetupRow
from app.database.models.strategy import Signal as SignalRow
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.trading import Position, Trade as TradeRow
from app.database.models.users import TradingPermission, User
from app.database.session import async_session_factory
from app.market.repository import get_candles
from app.strategy.dsl import StrategyDefinition
from app.market.timeframes import timeframe_to_minutes
from app.workers.auto_trade_worker import MAX_CANDLE_AGE_IN_BARS, AutoTradeSupervisor

pytestmark = pytest.mark.asyncio

_SWEEP_STRATEGY = {
    "name": "Previous-day sweep",
    "market": "TESTSYM",
    "timeframe": "15m",
    "direction": "bullish",
    # Reads `smc.recent_sweeps()`, which only exists once the analysed
    # window spans a day boundary -- the blueprint's canonical strategy.
    "conditions": [{"type": "liquidity_sweep", "lookback": 50}],
    "entry": {"type": "market"},
    "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
}


def _three_days_of_bars() -> list[tuple[datetime, float, float, float, float]]:
    """Three NSE sessions of 15m bars, day 3 sweeping day 2's low."""
    bars: list[tuple[datetime, float, float, float, float]] = []
    # Anchored so the newest bar has only just closed, rather than at a
    # fixed past date. The supervisor now refuses a candle more than
    # `MAX_CANDLE_AGE_IN_BARS` of its own timeframe late, because it trades
    # unattended and used to act on whatever the newest stored bar was --
    # and a fixed 2026-01-05 anchor is 256 days stale against the clock
    # these tests actually run under. The last bar here is
    # `base + 2 days + 24 * 15m`, so working backwards from that puts it at
    # roughly now.
    base = (
        datetime.now(timezone.utc).replace(second=0, microsecond=0)
        - timedelta(days=2)
        - timedelta(minutes=15 * 24)
    )
    price = 100.0
    for day in range(3):
        session = base + timedelta(days=day)
        for i in range(25):
            timestamp = session + timedelta(minutes=15 * i)
            if day == 2 and i == 5:
                bar = (price, price + 0.5, 88.0, price + 0.3)
            elif day == 1 and i == 12:
                bar = (price, price + 1.0, 90.0, price - 0.2)
            else:
                bar = (price, price + 1.2, price - 1.0, price + 0.4)
            bars.append((timestamp, *bar))
            price = bar[3]
    return bars


async def _seed(
    symbol: str, bar_count: int | None = None, *, from_end: bool = False
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed `bar_count` bars; `from_end` picks the newest rather than the oldest.

    Which end matters now that the supervisor refuses a stale candle. The
    series is anchored to finish at roughly now, so a large prefix
    (`bar_count=74` of 75) still ends about one bar ago and is fresh -- but
    a small prefix (`bar_count=2`) would be the two bars furthest in the
    past, nearly a day old, and would measure the freshness gate instead of
    the short-history path. An instrument with almost no history is one
    that only just started being tracked, so `from_end=True` is also the
    more faithful fixture for it.
    """
    bars = _three_days_of_bars()
    if bar_count is not None:
        bars = bars[-bar_count:] if from_end else bars[:bar_count]
    return await _seed_bars(symbol, bars)


async def _seed_bars(symbol: str, bars) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """The instrument/user/strategy rows, seeded with exactly `bars`."""
    async with async_session_factory() as db:
        instrument = Instrument(symbol=symbol, exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        for timestamp, o, h, low, c in bars:
            db.add(
                CandleRow(
                    instrument_id=instrument.id, timeframe="15m", timestamp=timestamp,
                    open=o, high=h, low=low, close=c, volume=1000,
                )
            )
        user = User(
            email=f"autohist-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Auto History",
            trading_permissions=[TradingPermission.AUTO_TRADE.value],
            auto_trading_enabled=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        strategy = StrategyRow(
            user_id=user.id, name="Sweep", definition=_SWEEP_STRATEGY,
            is_active=True, eligible_for_auto_trading=True,
        )
        db.add(strategy)
        await db.commit()
        await db.refresh(strategy)
        return instrument.id, user.id, strategy.id


async def _cleanup(instrument_id: uuid.UUID, user_id: uuid.UUID, strategy_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        for model in (TradeRow, Position, RiskEvent, Notification, AuditLog):
            await db.execute(delete(model).where(model.user_id == user_id))
        await db.execute(delete(SignalRow).where(SignalRow.instrument_id == instrument_id))
        await db.execute(delete(SetupRow).where(SetupRow.instrument_id == instrument_id))
        await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.id == strategy_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def _one_pass(instrument_id: uuid.UUID, user_id: uuid.UUID, strategy_id: uuid.UUID, supervisor):
    """Drive `_process` for exactly this triple.

    `run_once()` would iterate every active instrument in the database,
    which on a shared test database is hundreds of unrelated rows.
    """
    async with async_session_factory() as db:
        instrument = await db.get(Instrument, instrument_id)
        user = await db.get(User, user_id)
        strategy_row = await db.get(StrategyRow, strategy_id)
        definition = StrategyDefinition.model_validate(strategy_row.definition)
        outcome = await supervisor._process(db, user, strategy_row, definition, instrument)
        return outcome, supervisor._engines[(str(user_id), str(strategy_id), str(instrument_id))]


async def test_a_fresh_engine_analyses_the_instruments_stored_history(require_infra):
    # Regression test: the engine was created empty and fed one bar, so its
    # first evaluation analysed a 1-bar window against an instrument with
    # 75 stored bars.
    symbol = f"AHIS{uuid.uuid4().hex[:6].upper()}"
    instrument_id, user_id, strategy_id = await _seed(symbol)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        _, engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)

        async with async_session_factory() as db:
            stored = await get_candles(db, instrument_id, "15m")
        assert len(stored) == 75

        # Pre-fix this was 1.
        assert len(engine.candles) == len(stored)
        assert engine.candles[0].timestamp == stored[0].timestamp
        assert engine.candles[-1].timestamp == stored[-1].timestamp
    finally:
        await _cleanup(instrument_id, user_id, strategy_id)


async def test_the_analysed_window_yields_previous_day_liquidity(require_infra):
    # The part that does not warm up on its own: `detect_session_levels`
    # only emits a PREVIOUS_DAY_* / PREVIOUS_WEEK_* pool when the window
    # spans a bucket boundary, so a mid-session engine saw zero of them --
    # and therefore zero sweeps -- for the rest of that session.
    # `ConditionType.LIQUIDITY_SWEEP` reads exactly those pools.
    symbol = f"AHLQ{uuid.uuid4().hex[:6].upper()}"
    instrument_id, user_id, strategy_id = await _seed(symbol)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        _, engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)

        seeded = engine.smc_engine.analyze(engine.candles)
        previous_pools = [p for p in seeded.liquidity_pools if p.source_type.value.startswith("PREVIOUS")]
        assert previous_pools, "a window spanning three sessions must carry previous-day levels"
        assert seeded.recent_sweeps(), "and the day-3 sweep of day-2's low must be detected"

        # The same analyser over only the final bar -- the pre-fix window --
        # finds neither, which is why this is not a rounding difference.
        starved = engine.smc_engine.analyze(engine.candles[-1:])
        assert not [p for p in starved.liquidity_pools if p.source_type.value.startswith("PREVIOUS")]
        assert not starved.recent_sweeps()
    finally:
        await _cleanup(instrument_id, user_id, strategy_id)


async def test_a_second_pass_appends_rather_than_reseeding(require_infra):
    # The control: seeding must happen once. Re-seeding on every pass would
    # duplicate the history, and skipping the append would lose bars.
    symbol = f"AHAP{uuid.uuid4().hex[:6].upper()}"
    instrument_id, user_id, strategy_id = await _seed(symbol, bar_count=74)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        _, engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)
        assert len(engine.candles) == 74

        # A new bar arrives.
        last = _three_days_of_bars()[74]
        async with async_session_factory() as db:
            db.add(
                CandleRow(
                    instrument_id=instrument_id, timeframe="15m", timestamp=last[0],
                    open=last[1], high=last[2], low=last[3], close=last[4], volume=1000,
                )
            )
            await db.commit()

        _, engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)
        assert len(engine.candles) == 75, "the new bar is appended, the history is not re-seeded"
        timestamps = [c.timestamp for c in engine.candles]
        assert len(set(timestamps)) == 75, "no duplicated bars"
        assert timestamps == sorted(timestamps)
    finally:
        await _cleanup(instrument_id, user_id, strategy_id)


async def test_an_instrument_with_almost_no_history_still_works(require_infra):
    # Seeding must not break the short-history case: two stored bars means
    # the engine holds two and `on_candle`'s `len(self.candles) < 3` guard
    # is what declines, not an index error.
    symbol = f"AHSH{uuid.uuid4().hex[:6].upper()}"
    instrument_id, user_id, strategy_id = await _seed(symbol, bar_count=2, from_end=True)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        outcome, engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)
        assert len(engine.candles) == 2
        assert outcome is not None
        assert outcome["order_created"] is False
    finally:
        await _cleanup(instrument_id, user_id, strategy_id)


# --- and it must refuse a series that has gone stale ----------------------
#
# The call site for the freshness gate. `tests/workers/
# test_auto_trade_candle_freshness.py` proves what `candle_age_seconds`
# computes; nothing there proves the supervisor consults it, which is the
# half that has escaped injection in four earlier rounds.


async def _seed_at(symbol: str, *, minutes_ago: int) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """Seed the usual three-day series ending `minutes_ago` minutes back."""
    bars = _three_days_of_bars()
    newest = bars[-1][0]
    shift = datetime.now(timezone.utc).replace(second=0, microsecond=0) - timedelta(minutes=minutes_ago) - newest
    shifted = [(timestamp + shift, o, h, low, c) for timestamp, o, h, low, c in bars]
    return await _seed_bars(symbol, shifted)


async def test_the_supervisor_refuses_a_stale_series(require_infra):
    """Behavioural proof at the call site, and the round's whole point.

    This loop trades unattended. It used to act on whatever the newest
    stored candle was, and `PaperTradingEngine` reports
    `market_data_age_seconds=0.0` regardless — so `market_data_fresh` could
    not fail and the `RiskEvent` row recorded a gate that had "passed".

    A day-old newest bar is not an exotic state: `_last_candle_seen` is
    in-memory, so a worker restart makes the next pass act on it, and
    candles only arrive when an operator runs `POST /admin/backfill`.
    """
    symbol = f"AHST{uuid.uuid4().hex[:6].upper()}"
    stale_minutes = 24 * 60
    instrument_id, user_id, strategy_id = await _seed_at(symbol, minutes_ago=stale_minutes)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        outcome, engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)

        assert outcome is None, "a day-old bar must not be traded"
        assert engine.position_manager.get(engine.account_id, symbol) is None
        async with async_session_factory() as db:
            events = (
                await db.execute(select(RiskEvent).where(RiskEvent.user_id == user_id))
            ).scalars().all()
        assert events == [], "refusing before the risk engine means no fabricated audit row either"
    finally:
        await _cleanup(instrument_id, user_id, strategy_id)


async def test_the_supervisor_still_trades_a_current_series(require_infra):
    """Control, and the one that stops the gate becoming "never trade".

    Identical fixture, anchored so the newest bar has only just closed. If
    this failed too, the proof above would be showing that the supervisor
    refuses everything rather than that it refuses stale data.
    """
    symbol = f"AHFR{uuid.uuid4().hex[:6].upper()}"
    instrument_id, user_id, strategy_id = await _seed_at(symbol, minutes_ago=15)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        outcome, engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)

        assert outcome is not None, "a freshly closed bar must still be processed"
        assert len(engine.candles) > 1
    finally:
        await _cleanup(instrument_id, user_id, strategy_id)


async def test_a_bar_just_inside_the_allowance_is_still_traded(require_infra):
    """Behavioural proof at the call site, and injection is why it exists.

    Hard-coding the bar duration to `"1m"` in the supervisor's own call —
    ignoring `strategy.timeframe` — left the whole suite green. The two
    tests above could not see it: a bar 15 minutes old and a bar a day old
    land on the same side of the limit whichever duration is subtracted.

    This one sits where the difference decides the answer. A 15m bar
    stamped 59 minutes ago is 44 minutes late, inside the 45-minute
    allowance (`MAX_CANDLE_AGE_IN_BARS` × 15m), so it must trade. Subtract
    one minute instead of fifteen and it reads as 58 minutes late, and the
    supervisor refuses a feed that is perfectly healthy.
    """
    symbol = f"AHBD{uuid.uuid4().hex[:6].upper()}"
    bar_minutes = timeframe_to_minutes("15m")
    allowance_minutes = bar_minutes * MAX_CANDLE_AGE_IN_BARS
    # One minute inside the limit, measured the correct way.
    minutes_ago = bar_minutes + allowance_minutes - 1
    assert minutes_ago - bar_minutes < allowance_minutes
    assert minutes_ago - 1 > allowance_minutes, (
        "the fixture must straddle the two readings, or this test proves nothing"
    )

    instrument_id, user_id, strategy_id = await _seed_at(symbol, minutes_ago=minutes_ago)
    try:
        supervisor = AutoTradeSupervisor(timeframe="15m")
        outcome, _engine = await _one_pass(instrument_id, user_id, strategy_id, supervisor)
        assert outcome is not None, "a bar inside the allowance must still be processed"
    finally:
        await _cleanup(instrument_id, user_id, strategy_id)
