from dataclasses import replace

from app.ict.engine import ICTConfig, ICTEngine
from app.smc.engine import SMCConfig, SMCEngine
from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType, EntryConfig, RiskConfig, StrategyDefinition
from app.smc.types import PremiumDiscountZone
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


def test_direction_polarity_is_pinned_for_both_biases():
    # Nothing in this file exercised the bearish branch -- all four tests
    # above pin `direction="bullish"` -- so the polarity of the
    # `is_bullish` fork was never asserted at all. That is the fork the
    # unvalidated `direction` field routed into: any value that was not
    # literally "bullish" fell through to bearish, so a strategy declared
    # "LONG" produced stop *above* entry and target *below* it, and the
    # paper and autonomous engines opened a short from it.
    #
    # The DSL now rejects that input outright (tests/strategy/test_dsl.py),
    # so this pins the other half: each valid bias must bracket its entry
    # on the correct side, and the two must be genuine mirror images.
    smc = SMCEngine(SMCConfig(swing_length=2)).analyze(make_candles(BULLISH_SETUP))
    smc = replace(smc, dealing_range=PremiumDiscountZone(range_high=120.0, range_low=90.0))
    context = EvaluationContext(
        symbol="TESTSYM", timeframe="15m", timestamp=make_candles(BULLISH_SETUP)[-1].timestamp,
        current_price=100.0, smc=smc, ict=None,
    )

    def _evaluate(direction: str):
        strategy = StrategyDefinition(
            name="Polarity", market="TESTSYM", timeframe="15m", direction=direction,
            conditions=[], entry=EntryConfig(type="market"),
            risk=RiskConfig(risk_percent=1.0, minimum_rr=2.0),
        )
        return StrategyEngine().evaluate(strategy, context)

    long_signal = _evaluate("bullish")
    assert long_signal.matched
    assert long_signal.stop < long_signal.entry < long_signal.target

    short_signal = _evaluate("bearish")
    assert short_signal.matched
    assert short_signal.target < short_signal.entry < short_signal.stop

    # Mirror images about the same entry, at the same R multiple.
    assert long_signal.entry == short_signal.entry
    assert long_signal.risk_reward == short_signal.risk_reward


# A bullish FVG (99 .. 103) left unfilled, plus a confirmed swing low at 96
# and swing high at 111 so the dealing range exists -- both entry types can
# therefore resolve, which is what makes the substitution observable.
RETEST_VS_MARKET_SETUP = [
    (100, 101, 99.0, 100),
    (100, 102, 99.5, 101),
    (101, 103, 100.2, 102),
    (102, 102.5, 96.0, 98),   # swing low @ 96
    (98, 99, 96.5, 97),
    (97, 99.5, 96.8, 99),
    (99, 108, 103.0, 107),    # displacement -> bullish FVG 99 .. 103
    (107, 110, 106.0, 109),
    (109, 111, 107.0, 108),   # swing high @ 111
    (108, 109, 106.5, 107),
    (107, 108, 105.5, 106),
    (106, 107, 104.5, 105),   # current price 105, still above the gap
]


def test_a_case_typo_in_the_entry_type_no_longer_silently_becomes_a_market_entry():
    # Regression test: `EntryConfig.type` was a bare `str` and
    # `_resolve_entry_and_stop` read it as bare equality tests with an
    # implicit `else`, so "FVG_RETEST" -- one character of case away from
    # the real thing -- fell through to a *market* entry at the current
    # price with the stop at the dealing-range edge.
    #
    # Pre-fix, on these candles, that swap produced:
    #     fvg_retest  entry=102.75 stop=99.17 target=109.90  (3.48% stop)
    #     FVG_RETEST  entry=105.00 stop=96.00 target=123.00  (8.57% stop)
    # -- a different entry, a stop 2.5x further away, and a fill on this
    # candle where the retest strategy would not have traded at all
    # (`app/paper/engine.py` and `app/backtest/engine.py` only fill a
    # retest once `low <= entry <= high`).
    candles = make_candles(RETEST_VS_MARKET_SETUP)
    context = _build_context(candles)
    assert context.smc.dealing_range is not None, "fixture must reach the market-entry fallback"
    assert context.smc.unmitigated_fvgs(direction="BULLISH"), "fixture must reach the fvg_retest branch"

    def evaluate(entry_type: str):
        strategy = StrategyDefinition(
            name="Bullish FVG retest",
            market="TESTSYM",
            timeframe="15m",
            direction="bullish",
            conditions=[Condition(type=ConditionType.FVG, direction="bullish")],
            entry=EntryConfig(type=entry_type),
            risk=RiskConfig(risk_percent=0.5, minimum_rr=2.0),
        )
        return StrategyEngine().evaluate(strategy, context)

    canonical = evaluate("fvg_retest")
    typoed = evaluate("FVG_RETEST")
    market = evaluate("market")

    assert canonical.matched and typoed.matched and market.matched

    # The typo now resolves to the retest it names, not to the market entry.
    assert (typoed.entry, typoed.stop, typoed.target) == (canonical.entry, canonical.stop, canonical.target)
    assert (market.entry, market.stop) != (canonical.entry, canonical.stop)

    # And the substitution it used to make was not a rounding difference:
    # the market fallback enters at the current close with a stop at the
    # dealing-range low, which is a materially wider bracket.
    assert market.entry == candles[-1].close
    assert market.stop == context.smc.dealing_range.range_low
    assert abs(market.entry - market.stop) > 2 * abs(canonical.entry - canonical.stop)

    # The retest waits for price to trade back down to the gap; the market
    # substitute fills on this candle. That difference is the whole point.
    last = candles[-1]
    assert not last.low <= canonical.entry <= last.high
    assert last.low <= market.entry <= last.high
