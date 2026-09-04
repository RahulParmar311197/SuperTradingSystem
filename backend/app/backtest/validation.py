"""Train/validation/test period splitting and out-of-sample validation, to
guard against overfitting (blueprint §77-78: "A strategy that only works
on one historical period should not automatically be trusted.")."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.backtest.cost_model import CostModel
from app.backtest.engine import BacktestEngine
from app.backtest.metrics import BacktestMetricsResult, compute_metrics
from app.smc.types import Candle
from app.strategy.dsl import StrategyDefinition


@dataclass(slots=True)
class DatasetSplit:
    train: list[Candle]
    validation: list[Candle]
    test: list[Candle]


def split_periods(candles: list[Candle], train_pct: float = 0.6, validation_pct: float = 0.2) -> DatasetSplit:
    if not 0 < train_pct < 1 or not 0 < validation_pct < 1 or train_pct + validation_pct >= 1:
        raise ValueError("train_pct + validation_pct must be < 1, and each must be in (0, 1)")

    n = len(candles)
    train_end = int(n * train_pct)
    validation_end = train_end + int(n * validation_pct)

    return DatasetSplit(
        train=candles[:train_end],
        validation=candles[train_end:validation_end],
        test=candles[validation_end:],
    )


@dataclass(slots=True)
class OutOfSampleReport:
    train: BacktestMetricsResult
    validation: BacktestMetricsResult
    test: BacktestMetricsResult
    consistent: bool
    warnings: list[str] = field(default_factory=list)


def validate_out_of_sample(
    strategy: StrategyDefinition,
    candles: list[Candle],
    symbol: str,
    starting_capital: float = 100_000.0,
    cost_model: CostModel | None = None,
    train_pct: float = 0.6,
    validation_pct: float = 0.2,
) -> OutOfSampleReport:
    """Runs the same backtest engine independently over each split — never
    letting the strategy see validation/test data while "training" — and
    flags the simple, well-known overfitting smells: a strategy with no
    edge on unseen data, or one whose win rate collapses once it leaves
    the period it was tuned on. This is a heuristic, not a certification:
    read the three metric sets yourself before trusting a "consistent"
    result (blueprint §78).
    """
    split = split_periods(candles, train_pct, validation_pct)

    def run(period_candles: list[Candle]) -> BacktestMetricsResult:
        trades = BacktestEngine(strategy, starting_capital, cost_model).run(period_candles, symbol)
        return compute_metrics(trades, starting_capital)

    train_metrics = run(split.train)
    validation_metrics = run(split.validation)
    test_metrics = run(split.test)

    warnings: list[str] = []
    if test_metrics.total_trades == 0:
        warnings.append("No trades in the test period — cannot assess out-of-sample performance")
    elif test_metrics.net_profit <= 0:
        warnings.append("Strategy is not profitable on unseen (test) data")

    if train_metrics.total_trades > 0 and validation_metrics.total_trades > 0:
        if validation_metrics.win_rate < train_metrics.win_rate * 0.5:
            warnings.append("Win rate drops by more than half from training to validation — possible overfitting")

    return OutOfSampleReport(
        train=train_metrics,
        validation=validation_metrics,
        test=test_metrics,
        consistent=len(warnings) == 0,
        warnings=warnings,
    )
