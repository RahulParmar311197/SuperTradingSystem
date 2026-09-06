import uuid
from datetime import timedelta

import pytest
from sqlalchemy import delete

from app.brokers.mock import MockBroker
from app.database.models.instruments import Instrument, MarketType
from app.database.session import async_session_factory
from app.market.repository import upsert_candles
from app.paper.engine import PaperTradingEngine
from app.risk.limits import RiskLimits
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from app.trading.position_manager import PositionManager
from tests.smc.conftest import make_candles

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
    (104, 130, 104, 128),  # runs hard to target
]


def _strategy() -> StrategyDefinition:
    return StrategyDefinition(
        name="Bullish FVG retest",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[Condition(type=ConditionType.FVG, direction="bullish")],
        entry=EntryConfig(type="fvg_retest"),
        risk=RiskConfig(risk_percent=1.0, minimum_rr=2.0),
    )


@pytest.mark.asyncio
async def test_paper_engine_opens_and_closes_trade_through_full_stack():
    candles = make_candles(SETUP)
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM", starting_balance=100_000)

    order_created = False
    closed_pnl = None
    for candle in candles:
        outcome = await engine.on_candle(candle)
        order_created = order_created or outcome.order_created
        if outcome.closed_position_pnl is not None:
            closed_pnl = outcome.closed_position_pnl

    assert order_created is True
    assert closed_pnl is not None
    assert closed_pnl > 0
    assert engine.trades_today == 1

    position = engine.position_manager.get(engine.account_id, "TESTSYM")
    assert position.is_open is False


@pytest.mark.asyncio
async def test_strategy_allocation_limit_blocks_a_second_position_for_the_same_strategy():
    # Regression test: `on_candle` hardcoded `TradeRiskProposal.
    # strategy_allocation` to 0.0, so `RiskLimits.max_strategy_allocation_pct`
    # (blueprint §57's per-strategy circuit breaker) could never fail no
    # matter how much notional a strategy already had open. `PositionManager`
    # is shared across every engine driving the same account (see
    # AutoTradeSupervisor, one engine per (strategy, instrument) pair), but
    # `PositionRecord` had no way to attribute a position back to the
    # strategy that opened it -- so even a user who tightened this limit
    # got no protection against a single strategy repeatedly sizing up
    # across multiple instruments.
    strategy = _strategy()
    shared_positions = PositionManager()
    # Both engines run the *same* strategy row, so they share its
    # `strategies.id` -- exactly what AutoTradeSupervisor passes when it
    # builds one engine per (strategy, instrument) pair. Attribution keys
    # on this id, never on the DSL name, which PUT /strategies/{id} can
    # rewrite at any time.
    strategy_id = str(uuid.uuid4())
    # Starts permissive so the first (engine_a) trade is free to open --
    # tightened below once we know that trade's real notional.
    limits = RiskLimits(max_strategy_allocation_pct=100.0)
    engine_a = PaperTradingEngine(
        strategy, symbol="ALLOCA", account_id="shared-acct", starting_balance=100_000, risk_limits=limits, position_manager=shared_positions, strategy_id=strategy_id
    )
    engine_b = PaperTradingEngine(
        strategy, symbol="ALLOCB", account_id="shared-acct", starting_balance=100_000, risk_limits=limits, position_manager=shared_positions, strategy_id=strategy_id
    )

    order_created = False
    for candle in make_candles(SETUP)[:9]:  # stops before target -- leaves the position open
        outcome = await engine_a.on_candle(candle)
        order_created = order_created or outcome.order_created
    assert order_created is True

    position_a = shared_positions.get("shared-acct", "ALLOCA")
    assert position_a is not None and position_a.is_open
    assert position_a.strategy_id == strategy_id  # attributed by strategies.id, not the mutable DSL name

    # Tighten the limit to half of what engine_a's own trade alone already
    # used -- `strategy_allocation_pct` can only ever grow from here once
    # engine_b's own (nonnegative) position notional is added on top, so
    # this must fail regardless of exactly how that second notional is
    # sized. Nowhere near the untouched 0.0 the bug used to silently
    # substitute for `strategy_allocation`, which would never trip this
    # limit no matter how tightly the user configured it.
    first_notional_pct = abs(position_a.quantity) * position_a.average_price / 100_000 * 100
    limits.max_strategy_allocation_pct = first_notional_pct * 0.5

    outcome_b = None
    for candle in make_candles(SETUP)[:9]:
        outcome_b = await engine_b.on_candle(candle)
        if outcome_b.signal is not None and outcome_b.signal.matched:
            break

    assert outcome_b is not None
    assert outcome_b.risk_rejected_reason is not None
    assert outcome_b.risk_failed_check == "strategy_allocation_limit"

    position_b = shared_positions.get("shared-acct", "ALLOCB")
    assert position_b is None or not position_b.is_open


@pytest.mark.asyncio
async def test_correlated_exposure_limit_blocks_a_position_in_a_correlated_instrument(require_infra):
    # Regression test: `on_candle` never computed `TradeRiskProposal.
    # correlated_exposure` at all -- app/api/orders.py's live path does
    # this for real via `compute_correlated_exposure`, but this engine
    # (the one both app/api/paper.py's feed_candle and
    # AutoTradeSupervisor drive) left it at the dataclass default of 0.0,
    # so `correlated_exposure_limit` (blueprint §85) could never fail for
    # paper or autonomous trading no matter how concentrated the account
    # actually was in correlated instruments.
    strategy = _strategy()
    shared_positions = PositionManager()
    limits = RiskLimits(max_correlated_exposure_pct=100.0, correlation_threshold=0.5)
    engine_a = PaperTradingEngine(
        strategy, symbol="CORRA", account_id="corr-acct", starting_balance=100_000, risk_limits=limits, position_manager=shared_positions
    )
    engine_b = PaperTradingEngine(
        strategy, symbol="CORRB", account_id="corr-acct", starting_balance=100_000, risk_limits=limits, position_manager=shared_positions
    )

    async with async_session_factory() as db:
        instrument_a = Instrument(symbol="CORRA", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        instrument_b = Instrument(symbol="CORRB", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add_all([instrument_a, instrument_b])
        await db.flush()
        # Identical close history for both symbols -- perfectly correlated
        # (correlation == 1.0), comfortably above the 0.5 threshold above.
        candles = make_candles(SETUP)
        await upsert_candles(db, instrument_a.id, "15m", candles)
        await upsert_candles(db, instrument_b.id, "15m", candles)
        await db.commit()
        instrument_a_id, instrument_b_id = instrument_a.id, instrument_b.id

    try:
        order_created = False
        async with async_session_factory() as db:
            for candle in make_candles(SETUP)[:9]:  # stops before target -- leaves the position open
                outcome = await engine_a.on_candle(candle, db)
                order_created = order_created or outcome.order_created
        assert order_created is True

        position_a = shared_positions.get("corr-acct", "CORRA")
        assert position_a is not None and position_a.is_open

        # Tighten to half of engine_a's own notional share -- `correlated_exposure`
        # alone must already exceed this regardless of engine_b's own position size.
        first_notional_pct = abs(position_a.quantity) * position_a.average_price / 100_000 * 100
        limits.max_correlated_exposure_pct = first_notional_pct * 0.5

        outcome_b = None
        async with async_session_factory() as db:
            for candle in make_candles(SETUP)[:9]:
                outcome_b = await engine_b.on_candle(candle, db)
                if outcome_b.signal is not None and outcome_b.signal.matched:
                    break

        assert outcome_b is not None
        assert outcome_b.risk_rejected_reason is not None
        assert outcome_b.risk_failed_check == "correlated_exposure_limit"

        position_b = shared_positions.get("corr-acct", "CORRB")
        assert position_b is None or not position_b.is_open
    finally:
        async with async_session_factory() as db:
            from app.database.models.market import Candle as CandleRow

            await db.execute(delete(CandleRow).where(CandleRow.instrument_id.in_([instrument_a_id, instrument_b_id])))
            await db.execute(delete(Instrument).where(Instrument.id.in_([instrument_a_id, instrument_b_id])))
            await db.commit()


@pytest.mark.asyncio
async def test_paper_engine_respects_risk_kill_switch(require_infra):
    # Regression test: `on_candle` now refreshes `risk_engine.kill_switch`
    # from Redis on every candle (see app.risk.kill_switch.load_kill_switch_state)
    # so a kill triggered from any process takes effect immediately -- a
    # `KillSwitchState` set directly on the engine, as this test used to do,
    # would now just be overwritten by the next candle's refresh. Going
    # through the same Redis keys the admin endpoint writes is what
    # actually exercises that wiring.
    from app.core.redis import clear_global_kill, set_global_kill

    candles = make_candles(SETUP)
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM")
    await set_global_kill()
    try:
        saw_rejection = False
        for candle in candles:
            outcome = await engine.on_candle(candle)
            if outcome.risk_rejected_reason is not None:
                saw_rejection = True

        assert saw_rejection is True
        assert engine.trades_today == 0
    finally:
        await clear_global_kill()


@pytest.mark.asyncio
async def test_paper_engine_resets_daily_and_weekly_counters_at_boundaries():
    # Regression test: `trades_today`/`daily_pnl`/`weekly_pnl` used to
    # persist for the lifetime of the engine -- only a full worker restart
    # ever cleared them -- making RiskEngine.evaluate's
    # max_trades_per_day/daily_loss_limit/weekly_loss_limit checks
    # lifetime-of-process limits rather than the rolling daily/weekly
    # limits they're meant to be.
    candles = make_candles(SETUP)
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM", starting_balance=100_000)
    for candle in candles:
        await engine.on_candle(candle)

    assert engine.trades_today == 1
    assert engine.daily_pnl > 0
    assert engine.weekly_pnl == engine.daily_pnl

    # Candles start on a Monday (see tests/smc/conftest.py) -- a day later
    # is still the same ISO week, so only the daily counters reset.
    engine._roll_risk_window(candles[-1].timestamp + timedelta(days=1))
    assert engine.trades_today == 0
    assert engine.daily_pnl == 0.0
    assert engine.weekly_pnl > 0

    # A week later is a new ISO week -- weekly_pnl resets too.
    engine._roll_risk_window(candles[-1].timestamp + timedelta(days=7))
    assert engine.weekly_pnl == 0.0


@pytest.mark.asyncio
async def test_paper_engine_records_broker_rejections():
    # Regression test (write side): RiskEngine.evaluate's
    # `no_repeated_rejections` check (app/risk/engine.py, blueprint §57
    # "Repeated order rejection") reads `proposal.repeated_rejections`,
    # but PaperTradingEngine never set it -- always the TradeRiskProposal
    # default of 0 -- so a broker-rejected order never left any trace for
    # this check to see.
    candles = make_candles(SETUP)
    engine = PaperTradingEngine(
        _strategy(),
        symbol="TESTSYM",
        broker=MockBroker(starting_balance=100_000, reject_probability=1.0),
    )
    for candle in candles:
        await engine.on_candle(candle)

    # Every attempted entry was rejected by the 100%-reject-probability
    # broker (a rejected order never opens a position, so the strategy
    # keeps re-matching and re-attempting on later candles too) --
    # confirms a real broker rejection is reflected in the counter at all,
    # which is the exact thing that was missing.
    assert engine.repeated_rejections >= 1


@pytest.mark.asyncio
async def test_paper_engine_enforces_repeated_rejections_limit():
    # Regression test (read side): once `repeated_rejections` reaches
    # `RiskLimits.max_repeated_rejections` (default 3), the next signal
    # match must be blocked by `no_repeated_rejections` -- before the fix
    # this could never happen since the field was always 0.
    candles = make_candles(SETUP)
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM", starting_balance=100_000)
    engine.repeated_rejections = 3

    saw_rejection = False
    for candle in candles:
        outcome = await engine.on_candle(candle)
        if outcome.risk_failed_check == "no_repeated_rejections":
            saw_rejection = True

    assert saw_rejection is True


@pytest.mark.asyncio
async def test_paper_engine_enforces_abnormal_price_jump_limit():
    # Regression test: RiskEngine.evaluate's `no_abnormal_price_jump` check
    # (app/risk/engine.py, blueprint §57 "unexpected price jump") reads
    # `proposal.recent_price_jump_pct`, but PaperTradingEngine never
    # computed it -- always the TradeRiskProposal default of 0.0 -- so it
    # could never fail regardless of how violently the price moved between
    # candles. The SETUP fixture's own entry-candle jump (candle 6's close
    # 107 -> candle 7's close 109 -- the candle the strategy actually
    # matches and places an order on -- ~1.87%) is real, unmodified
    # fixture data -- lowering the limit below it is enough to prove the
    # wiring works without needing to synthesize an artificial price
    # spike.
    candles = make_candles(SETUP)
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM", starting_balance=100_000)
    engine.risk_engine.limits.max_price_jump_pct = 1.0

    saw_rejection = False
    for candle in candles:
        outcome = await engine.on_candle(candle)
        if outcome.risk_failed_check == "no_abnormal_price_jump":
            saw_rejection = True

    assert saw_rejection is True


@pytest.mark.asyncio
async def test_paper_engine_respects_strategy_level_kill_switch(require_infra):
    # Regression test: blueprint §58 requires three kill-switch levels
    # (global, account, strategy). Global and account both worked because
    # both sides key on `str(user.id)`, but the strategy level was a silent
    # no-op end to end: `POST /admin/kill-switch/strategy/{strategy_id}`
    # writes `kill:strategy:<strategies.id UUID>` -- the only strategy
    # identifier the API ever exposes -- while the engine asked Redis for
    # `kill:strategy:<StrategyDefinition.name>`, the free-text DSL name.
    # They never matched, so an admin stopping a runaway strategy watched it
    # keep placing orders.
    #
    # The sibling test above covers the global level, which is why this gap
    # survived: nothing exercised the one level that was broken.
    from app.core.redis import clear_strategy_kill, set_strategy_kill

    strategy_id = str(uuid.uuid4())  # a real strategies.id, as both callers now pass
    engine = PaperTradingEngine(_strategy(), symbol="TESTSYM", strategy_id=strategy_id)

    await set_strategy_kill(strategy_id)
    try:
        saw_rejection = False
        failed_check = None
        for candle in make_candles(SETUP):
            outcome = await engine.on_candle(candle)
            if outcome.risk_rejected_reason is not None:
                saw_rejection = True
                failed_check = failed_check or outcome.risk_failed_check

        assert saw_rejection is True
        assert failed_check == "kill_switch"
        assert engine.trades_today == 0
    finally:
        await clear_strategy_kill(strategy_id)

    # And the kill must be scoped: a different strategy's id must not stop
    # this one. `strategies.name` has no uniqueness constraint, so a
    # name-keyed kill would also have crossed between two users'
    # identically-named strategies.
    other_engine = PaperTradingEngine(_strategy(), symbol="TESTSYM", strategy_id=str(uuid.uuid4()))
    await set_strategy_kill(strategy_id)
    try:
        opened = False
        for candle in make_candles(SETUP):
            outcome = await other_engine.on_candle(candle)
            opened = opened or outcome.order_created
        assert opened is True, "a kill on one strategy must not stop a different strategy"
    finally:
        await clear_strategy_kill(strategy_id)
