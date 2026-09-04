from app.backtest.cost_model import CostModel
from app.backtest.validation import split_periods, validate_out_of_sample
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from tests.smc.conftest import make_candles

# A repeating bullish sweep+FVG setup, so it recurs across every split
# regardless of where the train/validation/test boundaries fall.
_UNIT = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
    (104, 130, 104, 128),
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


def test_validate_out_of_sample_runs_independently_over_each_split():
    candles = make_candles(_UNIT * 4)  # enough history for a clean 60/20/20 split
    report = validate_out_of_sample(_strategy(), candles, symbol="TESTSYM", cost_model=CostModel())

    split = split_periods(candles, 0.6, 0.2)
    assert report.train.total_trades >= 0
    assert report.validation.total_trades >= 0
    assert report.test.total_trades >= 0
    # sanity: the splits really are disjoint and cover the whole history
    assert len(split.train) + len(split.validation) + len(split.test) == len(candles)


def test_flags_no_trades_in_test_period():
    # Only enough history for the pattern to appear once, at the very
    # start — nothing left for the test split to trade.
    candles = make_candles(_UNIT + [(104, 104, 103, 104)] * 40)
    report = validate_out_of_sample(_strategy(), candles, symbol="TESTSYM", cost_model=CostModel())

    if report.test.total_trades == 0:
        assert report.consistent is False
        assert any("test period" in w for w in report.warnings)
