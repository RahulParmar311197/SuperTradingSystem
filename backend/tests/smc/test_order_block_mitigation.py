"""`OrderBlock.mitigated` must mean "spent", not "touched once".

Every consumer of the flag reads it as *still available to trade into*:
`SMCContext.active_order_blocks` filters on it, and through that filter it
drives `ConditionType.ORDER_BLOCK` (`app/strategy/evaluator.py`), the
`order_block_retest` entry (`app/strategy/engine.py`), the AI context
(`app/ai/context_builder.py`) and the chart overlay (`app/api/charts.py`).

The writer disagreed: `update_mitigation` set it on the first candle whose
range overlapped the block *at all*. That made `order_block_retest`
unfillable by construction. The entry is the block's midpoint, so
`bottom <= entry <= top`; the engines' fill gate is
`candle.low <= entry <= candle.high`; together those force
`candle.low <= top` and `candle.high >= bottom`, which is exactly the old
mitigation test. And `SMCEngine.analyze` re-runs mitigation over the current
bar before the strategy is evaluated, so the bar that could fill the retest
was always the bar that had just removed the block. A merely adjacent bar
killed it sooner still.

The sibling zone type already had this right: `app/smc/fvg.py` accumulates a
graded `filled_percentage` and only mitigates at full fill or invalidation.

Why the existing tests missed it: both retest fill-gate tests
(`tests/backtest/test_engine.py::test_backtest_only_fills_a_retest_entry_once_price_actually_trades_there`
and `tests/paper/test_engine.py::test_a_retest_entry_fills_at_its_level_not_at_the_candle_close`)
assert the generic "a retest entry" contract but use `EntryConfig(type="fvg_retest")`
only -- and FVG grading means a midpoint touch is a partial fill, so their
fixtures structurally cannot reach the order-block branch (shape b).
`tests/smc/test_order_blocks.py` never touched `mitigated` at all, and
`order_block_retest` had no positive-path coverage anywhere in the suite --
its only other appearance is as the deliberately-never-matching definition in
`tests/workers/test_auto_trade_worker.py` (shape f).
"""

import pytest

from app.backtest.engine import BacktestEngine
from app.ict.engine import ICTConfig, ICTEngine
from app.smc.engine import SMCConfig, SMCEngine
from app.smc.order_blocks import update_mitigation
from app.smc.types import Direction, OrderBlock
from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from app.strategy.engine import StrategyEngine
from tests.smc.conftest import make_candles
from tests.smc.test_swings import OHLC

# The suite's own swing fixture ends with an unmitigated BEARISH order block
# at [102, 107] (midpoint 104.5), caused by the structure break at bar 10.
# These five bars rally back into it: bar 12 grazes its lower edge, bar 13 is
# the genuine retest through the midpoint, and bar 14 rolls over.
RETEST = OHLC + [
    (96, 99, 95, 98),
    (98, 102, 97, 101),
    (101, 106, 100, 103),
    (103, 104, 100, 101),
    (101, 102, 97, 98),
]
BLOCK_MIDPOINT = 104.5
RETEST_BAR = 13


def _strategy(entry_type: str = "order_block_retest") -> StrategyDefinition:
    return StrategyDefinition(
        name="Bearish order-block retest",
        market="TESTSYM",
        timeframe="15m",
        direction="bearish",
        conditions=[Condition(type=ConditionType.BOS, direction="bearish")],
        entry=EntryConfig(type=entry_type),
        risk=RiskConfig(risk_percent=1.0, minimum_rr=2.0),
    )


def _bearish_block(candles):
    blocks = [b for b in SMCEngine(SMCConfig()).analyze(candles).order_blocks if b.direction is Direction.BEARISH]
    assert blocks, "fixture must produce a bearish order block"
    return blocks[0]


def _evaluate_at(candles, index):
    window = candles[: index + 1]
    smc = SMCEngine(SMCConfig()).analyze(window)
    context = EvaluationContext(
        symbol="TESTSYM",
        timeframe="15m",
        timestamp=window[-1].timestamp,
        current_price=window[-1].close,
        smc=smc,
        ict=ICTEngine(ICTConfig()).analyze(window),
        current_index=len(window) - 1,
    )
    return StrategyEngine().evaluate(_strategy(), context), smc, window[-1]


def test_the_block_survives_the_bar_that_retests_its_midpoint():
    # Regression test: the retest bar marked the block mitigated, so
    # `active_order_blocks` was empty at the exact moment the entry needed it.
    candles = make_candles(RETEST)
    result, smc, candle = _evaluate_at(candles, RETEST_BAR)

    assert candle.low <= BLOCK_MIDPOINT <= candle.high, "this bar really does trade through the level"
    assert smc.active_order_blocks("BEARISH"), "the block must still be tradable on its own retest bar"
    assert result.entry == pytest.approx(BLOCK_MIDPOINT)
    # The fill gate the backtest and paper engines apply.
    assert candle.low <= result.entry <= candle.high


def test_a_bar_that_only_grazes_the_edge_does_not_consume_the_block():
    # Bar 12 spans (97, 102) and touches the block's lower edge exactly. It
    # never reaches the midpoint, yet it used to kill the block a full bar
    # before the real retest.
    candles = make_candles(RETEST)
    result, smc, candle = _evaluate_at(candles, RETEST_BAR - 1)

    assert candle.high == 102.0
    assert not (candle.low <= BLOCK_MIDPOINT <= candle.high), "this bar does not reach the level"
    assert smc.active_order_blocks("BEARISH")
    assert result.entry == pytest.approx(BLOCK_MIDPOINT)


def test_an_order_block_retest_strategy_actually_books_a_trade():
    # The end-to-end consequence. Pre-fix this returned zero trades for any
    # input, so a user backtesting an `order_block_retest` strategy read
    # "no edge" for a strategy the platform could not execute.
    candles = make_candles(RETEST)
    trades = BacktestEngine(_strategy()).run(candles, "TESTSYM")

    assert len(trades) == 1
    assert trades[0].entry_price == pytest.approx(BLOCK_MIDPOINT)


def test_fill_is_graded_the_way_the_fvg_sibling_grades_it():
    candles = make_candles(RETEST)
    block = _bearish_block(candles)

    # The retest reaches 106 into a [102, 107] block: 4 of 5 points.
    assert block.filled_percentage == pytest.approx(0.8)
    assert block.mitigated is False
    assert block.invalidated is False
    assert block.mitigated_index is None


def test_trading_fully_through_the_block_does_mitigate_it():
    # The control: `mitigated` must still become True for a block price has
    # actually consumed, or the flag would never retire anything.
    candles = make_candles(RETEST + [(98, 109, 97, 108)])
    block = _bearish_block(candles)

    assert block.filled_percentage == pytest.approx(1.0)
    assert block.mitigated is True
    assert block.mitigated_index == len(candles) - 1
    assert not SMCEngine(SMCConfig()).analyze(candles).active_order_blocks("BEARISH")


def test_a_candle_engulfing_the_block_invalidates_it():
    candles = make_candles(RETEST + [(98, 110, 98, 109)])
    block = _bearish_block(candles)

    assert block.invalidated is True
    assert block.mitigated is True


def test_a_mitigated_block_stops_resolving_an_entry():
    # Once the block really is spent, `order_block_retest` must go back to
    # returning no entry rather than naming a level nobody is defending.
    candles = make_candles(RETEST + [(98, 109, 97, 108), (108, 110, 107, 109)])
    result, smc, _ = _evaluate_at(candles, len(candles) - 1)

    assert not smc.active_order_blocks("BEARISH")
    assert result.entry is None


def test_an_untouched_block_reports_no_fill():
    # The other control: a block price has not returned to at all must read
    # 0.0, not some residue of the bars that merely passed nearby.
    candles = make_candles(OHLC)
    block = _bearish_block(candles)

    assert block.filled_percentage == 0.0
    assert block.mitigated is False


def test_direction_decides_which_edge_the_fill_is_measured_from():
    # A bearish block is supply above price, filled upward from its bottom; a
    # bullish block is demand below price, filled downward from its top. This
    # mirrors `app/smc/fvg.py`, and it is the one branch in the new grading
    # that nothing else here exercises.
    #
    # Over the same candles and the same [102, 107] geometry the two must
    # disagree: bar 13 spans (100, 106), so it reaches 4 points up from the
    # bottom but a full 5 points down from the top.
    candles = make_candles(RETEST)
    bearish = _bearish_block(candles)
    bullish = OrderBlock(
        direction=Direction.BULLISH,
        top=bearish.top,
        bottom=bearish.bottom,
        created_index=bearish.created_index,
        created_at=bearish.created_at,
        strength=bearish.strength,
        caused_event_index=bearish.caused_event_index,
    )

    update_mitigation(candles, [bullish])

    assert bearish.filled_percentage == pytest.approx(0.8)
    assert bearish.mitigated is False
    assert bullish.filled_percentage == pytest.approx(1.0)
    assert bullish.mitigated is True
