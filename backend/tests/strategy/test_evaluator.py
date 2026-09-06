from app.ict.engine import ICTConfig, ICTEngine
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType
from app.strategy.evaluator import evaluate_condition
from tests.smc.conftest import make_candles
from tests.smc.test_liquidity import EQUAL_HIGHS
from tests.smc.test_swings import OHLC


def _context(candles, smc, current_index: int) -> EvaluationContext:
    ict = ICTEngine(ICTConfig()).analyze(candles)
    return EvaluationContext(
        symbol="TESTSYM",
        timeframe="15m",
        timestamp=candles[-1].timestamp,
        current_price=candles[-1].close,
        smc=smc,
        ict=ict,
        current_index=current_index,
    )


def test_bos_condition_expires_after_its_lookback_window():
    # Regression test: `Condition.lookback` (app/strategy/dsl.py) documents
    # itself as "how many recent candles/events count as recent for
    # event-type conditions", but `evaluate_condition` never read it -- a
    # BOS/CHoCH/MSS structure event, which happens exactly once in a
    # candle's history, matched forever afterward with no expiry, unlike
    # persistent-state conditions (FVG/order block) where "still
    # unmitigated" is a real, ongoing state.
    candles = make_candles(OHLC)
    smc = SMCEngine(SMCConfig(swing_length=2)).analyze(candles)
    bos = next(e for e in smc.structure_events if e.event_type.value == "BOS")
    assert bos.index == 7  # pinned by tests/smc/test_structure.py

    condition = Condition(type=ConditionType.BOS, direction="bullish")  # lookback defaults to 5

    # Evaluated shortly after the break fired -- still within the window.
    fresh = _context(candles, smc, current_index=bos.index + 4)
    assert evaluate_condition(condition, fresh) is True

    # Evaluated many candles later -- the exact same, now-stale BOS must no
    # longer count as a "recent" break.
    stale = _context(candles, smc, current_index=bos.index + 20)
    assert evaluate_condition(condition, stale) is False


def test_liquidity_sweep_condition_expires_after_its_lookback_window():
    candles = make_candles(EQUAL_HIGHS)
    smc = SMCEngine(SMCConfig(swing_length=1, equal_level_tolerance_pct=0.5)).analyze(candles)
    pool = next(p for p in smc.liquidity_pools if p.swept)
    assert pool.swept_index is not None

    condition = Condition(type=ConditionType.LIQUIDITY_SWEEP, side="buy")  # lookback defaults to 5

    fresh = _context(candles, smc, current_index=pool.swept_index + 4)
    assert evaluate_condition(condition, fresh) is True

    stale = _context(candles, smc, current_index=pool.swept_index + 20)
    assert evaluate_condition(condition, stale) is False
