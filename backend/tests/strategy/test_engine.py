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


def test_market_entry_below_the_dealing_range_low_does_not_emit_an_inverted_long():
    # Regression test: the default "market" entry type resolves to
    # `entry=current_price, stop=dealing_range.range_low` for a long, and the
    # dealing range is only the most recent confirmed swing high/low -- it is
    # not guaranteed to bracket the current price. Once price drifts below the
    # range low, this produced a matched LONG whose stop sat *above* its entry.
    # Nothing downstream could catch it (`RiskEngine` measures the stop with
    # `abs(entry - stop)` and `TradeRiskProposal` carries no direction), and
    # `_maybe_exit`/`_check_exit` then read `candle.low <= stop` as a stop-loss
    # hit on the very next candle -- filling above the entry and booking a
    # guaranteed profit labelled `stop_loss`, which also inflated `daily_pnl`
    # and so loosened the daily-loss halt.
    #
    # A zigzag establishes a confirmed swing high and swing low, then price
    # drifts steadily below that swing low. The jitter keeps every high/low
    # unique so the strict-uniqueness swing detector actually confirms them.
    closes = [100, 96, 92, 97, 103, 110, 116, 112, 107, 103, 99, 104, 110, 117, 124, 119, 113, 106, 99, 92, 85]
    ohlc = []
    previous = None
    for index, close in enumerate(closes):
        open_ = previous if previous is not None else close
        jitter = index * 0.013
        ohlc.append((open_, max(open_, close) + 1 + jitter, min(open_, close) - 1 - jitter, close))
        previous = close

    candles = make_candles(ohlc)
    context = _build_context(candles)

    # The setup this test depends on: price is genuinely below the range low.
    assert context.smc.dealing_range is not None
    assert context.current_price < context.smc.dealing_range.range_low

    strategy = StrategyDefinition(
        name="Market entry long",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[],
        entry=EntryConfig(),  # default: "market"
        risk=RiskConfig(risk_percent=1.0, minimum_rr=2.0),
    )

    result = StrategyEngine().evaluate(strategy, context)

    assert result.matched is False
    assert "stop_on_wrong_side_of_entry" in result.missing
    # Nothing downstream should ever see an inverted bracket.
    assert result.entry is None and result.stop is None


def test_market_entry_inside_the_dealing_range_still_emits_a_valid_long():
    # The guard above must not suppress the ordinary case: with price above
    # the range low, a market-entry long is still a well-formed signal.
    closes = [100, 96, 92, 97, 103, 110, 116, 112, 107, 103, 99, 104, 110, 117, 124, 119, 113]
    ohlc = []
    previous = None
    for index, close in enumerate(closes):
        open_ = previous if previous is not None else close
        jitter = index * 0.013
        ohlc.append((open_, max(open_, close) + 1 + jitter, min(open_, close) - 1 - jitter, close))
        previous = close

    candles = make_candles(ohlc)
    context = _build_context(candles)

    assert context.smc.dealing_range is not None
    assert context.current_price > context.smc.dealing_range.range_low

    strategy = StrategyDefinition(
        name="Market entry long",
        market="TESTSYM",
        timeframe="15m",
        direction="bullish",
        conditions=[],
        entry=EntryConfig(),
        risk=RiskConfig(risk_percent=1.0, minimum_rr=2.0),
    )

    result = StrategyEngine().evaluate(strategy, context)

    assert result.matched is True
    assert result.stop < result.entry < result.target


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
