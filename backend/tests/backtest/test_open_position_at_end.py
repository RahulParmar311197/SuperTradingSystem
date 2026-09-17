"""A position the data ran out on used to vanish.

`BacktestEngine.run` kept `open_trade` local and returned only closed
trades, so a run that opened a position and held it to the last candle
came back as `[]` — indistinguishable from a strategy that never fired,
with `compute_metrics` then reporting `total_trades: 0`.

The bias runs one way. A strategy whose stop is wide enough that the data
window ends before it is touched has exactly those trades omitted while
its winners closed and counted. `ReplayEngine` has always exposed its
`open_trade` (both `app/api/replay.py` and `app/replay/persistence.py`
read it); the backtester kept the same state private and dropped it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.engine import BacktestEngine, OpenBacktestPosition
from app.smc.types import Candle
from app.strategy.dsl import StrategyDefinition

BASE = datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc)


@dataclass
class _Match:
    matched: bool
    direction: str = "bullish"
    entry: float = 0.0
    stop: float = 0.0
    target: float = 0.0


class _FiresOnce:
    """Matches on the first evaluation only, so exactly one position opens
    and the data then runs out before stop or target is touched."""

    def __init__(self, stop_offset: float = 50.0, target_offset: float = 50.0) -> None:
        self.calls = 0
        self.stop_offset = stop_offset
        self.target_offset = target_offset

    def evaluate(self, strategy, context):
        self.calls += 1
        if self.calls == 1:
            return _Match(
                True,
                "bullish",
                entry=context.current_price,
                stop=context.current_price - self.stop_offset,
                target=context.current_price + self.target_offset,
            )
        return _Match(False)


class _FiresOnceShort(_FiresOnce):
    """Same, but short: stop above and target below the entry."""

    def evaluate(self, strategy, context):
        self.calls += 1
        if self.calls == 1:
            return _Match(
                True,
                "bearish",
                entry=context.current_price,
                stop=context.current_price + self.stop_offset,
                target=context.current_price - self.target_offset,
            )
        return _Match(False)


class _FiresTwice:
    """Matches twice: a tight target that the candles reach, then a wide
    one that survives to the end. A run with both a closed trade and a
    held one is the shape the bias actually takes."""

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, strategy, context):
        self.calls += 1
        if self.calls == 1:
            return _Match(True, "bullish", context.current_price, context.current_price - 50.0, context.current_price + 0.5)
        if self.calls == 2:
            return _Match(True, "bullish", context.current_price, context.current_price - 50.0, context.current_price + 50.0)
        return _Match(False)


def _strategy() -> StrategyDefinition:
    return StrategyDefinition.model_validate(
        {
            "name": "open-at-end",
            "timeframe": "15m",
            "market": "NSE",
            "conditions": [{"type": "fvg"}],
            "entry": {"type": "market"},
            "risk": {"risk_percent": 1.0, "reward_ratio": 2.0},
        }
    )


def _flat_candles(n: int = 10, close: float = 100.0) -> list[Candle]:
    # Never reaches a stop 50 below or a target 50 above.
    return [Candle(BASE + timedelta(minutes=15 * i), close, close + 1, close - 1, close, 1000) for i in range(n)]


def _engine_that_holds() -> BacktestEngine:
    engine = BacktestEngine(_strategy(), starting_capital=100_000.0)
    engine.strategy_engine = _FiresOnce()
    return engine


# --- the omission ---------------------------------------------------------


def test_a_position_open_at_the_end_is_reported():
    engine = _engine_that_holds()
    trades = engine.run(_flat_candles(), symbol="TEST")

    # Still not in the closed trades: it did not close, and the metrics are
    # computed over closed trades on purpose.
    assert trades == []
    # But no longer invisible.
    assert isinstance(engine.open_trade, OpenBacktestPosition)
    assert engine.open_trade.direction == "LONG"
    assert engine.open_trade.entry_price == 100.0
    assert engine.open_trade.stop == 50.0
    assert engine.open_trade.quantity > 0


def test_the_reported_position_is_marked_at_the_last_close():
    engine = _engine_that_holds()
    candles = _flat_candles()
    # Drift the final candle up without reaching the target.
    candles[-1] = Candle(candles[-1].timestamp, 120.0, 121.0, 119.0, 120.0, 1000)
    engine.run(candles, symbol="TEST")

    open_position = engine.open_trade
    assert open_position is not None
    assert open_position.last_price == 120.0
    # Gross, at the close: entry 100, 20 points on `quantity` units.
    assert open_position.unrealized_pnl == pytest.approx(20.0 * open_position.quantity)


def test_an_open_loser_is_reported_not_quietly_dropped():
    # The direction of the bias. A stop wide enough to survive the window
    # is exactly the trade that used to disappear.
    engine = _engine_that_holds()
    candles = _flat_candles()
    candles[-1] = Candle(candles[-1].timestamp, 80.0, 81.0, 79.0, 80.0, 1000)
    engine.run(candles, symbol="TEST")

    assert engine.open_trade is not None
    assert engine.open_trade.unrealized_pnl < 0


# --- controls -------------------------------------------------------------


def test_a_run_that_ends_flat_reports_no_open_position():
    engine = BacktestEngine(_strategy(), starting_capital=100_000.0)

    class _NeverFires:
        def evaluate(self, strategy, context):
            return _Match(False)

    engine.strategy_engine = _NeverFires()
    assert engine.run(_flat_candles(), symbol="TEST") == []
    assert engine.open_trade is None


def test_a_closed_trade_still_closes_and_leaves_nothing_open():
    # Control: the ordinary path must be untouched. A tight target the
    # candles do reach closes normally and leaves no open position.
    engine = BacktestEngine(_strategy(), starting_capital=100_000.0)
    engine.strategy_engine = _FiresOnce(stop_offset=50.0, target_offset=0.5)
    trades = engine.run(_flat_candles(), symbol="TEST")

    assert len(trades) == 1
    assert engine.open_trade is None


def test_open_trade_is_reset_between_runs():
    # It is instance state, so a second run must not inherit the first
    # run's leftover position.
    engine = _engine_that_holds()
    engine.run(_flat_candles(), symbol="TEST")
    assert engine.open_trade is not None

    engine.strategy_engine = _FiresOnce(stop_offset=50.0, target_offset=0.5)
    engine.run(_flat_candles(), symbol="TEST")
    assert engine.open_trade is None


def test_a_held_short_is_marked_in_the_right_direction():
    # The mark carries a sign. A short held while price rises is losing;
    # reporting it as a gain would be worse than dropping it, because it
    # reads as a result.
    engine = BacktestEngine(_strategy(), starting_capital=100_000.0)
    engine.strategy_engine = _FiresOnceShort()
    candles = _flat_candles()
    candles[-1] = Candle(candles[-1].timestamp, 120.0, 121.0, 119.0, 120.0, 1000)
    engine.run(candles, symbol="TEST")

    open_position = engine.open_trade
    assert open_position is not None
    assert open_position.direction == "SHORT"
    assert open_position.last_price == 120.0
    # Entry 100, marked at 120: a short is 20 points down, not up.
    assert open_position.unrealized_pnl == pytest.approx(-20.0 * open_position.quantity)


def test_a_position_held_after_an_earlier_trade_closed_is_still_reported():
    # The flattering case in full: one winner closed and counted, one
    # position still open when the data ran out. Reporting the open one
    # only on runs that closed nothing would hide exactly this.
    engine = BacktestEngine(_strategy(), starting_capital=100_000.0)
    engine.strategy_engine = _FiresTwice()
    trades = engine.run(_flat_candles(), symbol="TEST")

    assert len(trades) == 1
    assert engine.open_trade is not None
    assert engine.open_trade.direction == "LONG"
