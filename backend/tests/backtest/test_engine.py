from app.backtest.cost_model import CostModel
from app.backtest.engine import BacktestEngine
from app.backtest.metrics import compute_metrics
from app.backtest.validation import split_periods
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from tests.smc.conftest import make_candles

# Reuse the bullish sweep+FVG dataset from the strategy engine tests, extended
# with a clean run to the target so the backtest produces a closed trade.
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


def test_backtest_opens_and_closes_a_trade_on_target_hit():
    candles = make_candles(SETUP)
    engine = BacktestEngine(_strategy(), starting_capital=100_000, cost_model=CostModel())

    trades = engine.run(candles, symbol="TESTSYM")

    assert len(trades) == 1
    trade = trades[0]
    assert trade.direction == "LONG"
    assert trade.pnl > 0


def test_costs_reduce_pnl_versus_zero_cost_baseline():
    candles = make_candles(SETUP)
    free = BacktestEngine(_strategy(), cost_model=CostModel()).run(candles, "TESTSYM")
    costly = BacktestEngine(
        _strategy(), cost_model=CostModel(brokerage_pct=0.1, slippage_pct=0.1, taxes_pct=0.05)
    ).run(candles, "TESTSYM")

    assert costly[0].pnl < free[0].pnl


def test_metrics_report_matches_blueprint_fields():
    candles = make_candles(SETUP)
    trades = BacktestEngine(_strategy()).run(candles, "TESTSYM")
    metrics = compute_metrics(trades, starting_capital=100_000)

    assert metrics.total_trades == 1
    assert metrics.win_rate == 1.0
    assert metrics.net_profit > 0
    assert metrics.long_trades == 1
    assert metrics.short_trades == 0
    assert len(metrics.equity_curve) == metrics.total_trades + 1


def test_split_periods_respects_ratios():
    candles = make_candles(SETUP * 3)  # more candles for a cleaner split
    split = split_periods(candles, train_pct=0.6, validation_pct=0.2)

    assert len(split.train) + len(split.validation) + len(split.test) == len(candles)
    assert len(split.train) > len(split.validation)
