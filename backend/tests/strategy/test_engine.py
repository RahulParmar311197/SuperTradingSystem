from app.ict.engine import ICTConfig, ICTEngine
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from app.strategy.engine import StrategyEngine
from tests.smc.conftest import make_candles

# A clean bullish sweep -> FVG setup: price dips to sweep a low, then
# displaces upward leaving an FVG that price later retests.
BULLISH_SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),  # swing low candidate area
    (102, 102, 97, 98),  # sweep candle: wicks below prior low
    (98, 99, 96, 97),  # swing low @ 96
    (97, 100, 96, 99),
    (99, 108, 99, 107),  # displacement candle -> creates FVG with candle below/above
    (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces back toward the FVG
]


def _build_context(candles):
    smc = SMCEngine(SMCConfig(swing_length=2)).analyze(candles)
    ict = ICTEngine(ICTConfig()).analyze(candles)
    return EvaluationContext(
        symbol="TESTSYM",
        timeframe="15m",
        timestamp=candles[-1].timestamp,
        current_price=candles[-1].close,
        smc=smc,
        ict=ict,
    )


def test_strategy_with_only_fvg_condition_matches_when_gap_present():
    candles = make_candles(BULLISH_SETUP)
    context = _build_context(candles)

    strategy = StrategyDefinition(
        name="Bullish FVG retest",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[Condition(type=ConditionType.FVG, direction="bullish")],
        entry=EntryConfig(type="fvg_retest"),
        risk=RiskConfig(risk_percent=0.5, minimum_rr=2.0),
    )

    result = StrategyEngine().evaluate(strategy, context)

    if context.smc.unmitigated_fvgs(direction="BULLISH"):
        assert result.matched is True
        assert result.direction == "bullish"
        assert result.risk_reward == 2.0
        assert result.stop < result.entry < result.target
        assert 0 <= result.score <= 100
    else:
        assert result.matched is False


def test_strategy_fails_when_required_condition_missing():
    candles = make_candles(BULLISH_SETUP)
    context = _build_context(candles)

    strategy = StrategyDefinition(
        name="Impossible setup",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[
            Condition(type=ConditionType.FVG, direction="bearish"),
            Condition(type=ConditionType.PREMIUM_DISCOUNT, zone="premium"),
        ],
    )

    result = StrategyEngine().evaluate(strategy, context)
    assert result.matched is False
    assert result.missing
