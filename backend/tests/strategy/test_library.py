"""Every shipped strategy must actually produce a signal.

A strategy library whose entries never match is decoration. The blueprint's
own §34 example was exactly that: it appeared nowhere in the codebase, was
never run, and could not fire, because the DSL gave every condition a flat
5-bar `lookback`. A market-structure shift is a sequence -- a CHoCH
breaking a confirmed swing, then a same-direction BOS breaking a later one,
each waiting `swing_length` bars for confirmation -- so it cannot exist
within 5 bars of the sweep that caused it, and the sweep had always expired
by the time the MSS appeared.

The corpus below is a set of seeded random walks. It is not market data and
proves nothing about profitability; it is a lower bound on expressibility --
price action varied enough that a setup which can occur, does.
"""

import random
from datetime import datetime, timedelta, timezone

import pytest

from app.ict.engine import ICTConfig, ICTEngine
from app.smc.engine import SMCConfig, SMCEngine
from app.smc.types import Candle
from app.strategy.context import EvaluationContext
from app.strategy.dsl import Condition, ConditionType, StrategyDefinition
from app.strategy.engine import StrategyEngine
from app.strategy.library import LIBRARY, load, load_all

_BASE = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)


def _walk(seed: int, bars: int = 150) -> list[Candle]:
    rng = random.Random(seed)
    price = 100.0
    candles = []
    for i in range(bars):
        nxt = max(5.0, price + rng.gauss(0, 1.4))
        high = max(price, nxt) + abs(rng.gauss(0, 0.6))
        low = min(price, nxt) - abs(rng.gauss(0, 0.6))
        # Spread across days so session-level liquidity pools can form;
        # a single day has no previous-day high/low to sweep.
        timestamp = _BASE + timedelta(days=i // 25, minutes=15 * (i % 25))
        candles.append(Candle(timestamp, price, high, low, nxt, 1000))
        price = nxt
    return candles


def _contexts(seeds=range(12), step: int = 4) -> list[EvaluationContext]:
    out = []
    for seed in seeds:
        candles = _walk(seed)
        for i in range(6, len(candles), step):
            visible = candles[: i + 1]
            out.append(
                EvaluationContext(
                    symbol="NIFTY",
                    timeframe="15m",
                    timestamp=candles[i].timestamp,
                    current_price=candles[i].close,
                    smc=SMCEngine(SMCConfig()).analyze(visible),
                    ict=ICTEngine(ICTConfig()).analyze(visible),
                    current_index=i,
                )
            )
    return out


@pytest.fixture(scope="module")
def corpus() -> list[EvaluationContext]:
    return _contexts()


# --- the library is real ---------------------------------------------------


@pytest.mark.parametrize("key", sorted(LIBRARY))
def test_every_library_strategy_validates(key):
    strategy = load(key)
    assert strategy.name
    assert strategy.conditions


@pytest.mark.parametrize("key", sorted(LIBRARY))
def test_every_library_strategy_actually_produces_a_signal(key, corpus):
    engine = StrategyEngine()
    matches = [r for r in (engine.evaluate(load(key), c) for c in corpus) if r.matched]
    assert matches, f"library strategy {key!r} never matched -- it would ship as decoration"
    # A match nothing can act on is no better: the entry path must resolve
    # to a real bracket, not None.
    first = matches[0]
    assert first.entry is not None and first.stop is not None
    assert first.entry != first.stop


def test_load_rejects_an_unknown_key():
    with pytest.raises(KeyError):
        load("no_such_strategy")


def test_load_all_returns_every_entry():
    assert set(load_all()) == set(LIBRARY)


# --- the blueprint's own example, and why it used to be dead ---------------


def test_the_blueprint_example_fires_with_default_lookbacks(corpus):
    # §34 as the blueprint prints it: no `lookback` stated anywhere.
    engine = StrategyEngine()
    matched = sum(1 for c in corpus if engine.evaluate(load("bullish_liquidity_sweep"), c).matched)
    assert matched > 0, "the blueprint's flagship strategy must fire out of the box"


def test_the_blueprint_example_is_starved_by_a_five_bar_lookback(corpus):
    # The regression this guards: with the old flat default, the sweep
    # expired before the MSS it caused could confirm. Same strategy, same
    # corpus, only the window changed -- so the comparison isolates it.
    engine = StrategyEngine()
    strategy = load("bullish_liquidity_sweep")
    starved = StrategyDefinition.model_validate(
        {
            **strategy.model_dump(mode="json"),
            "conditions": [{**c.model_dump(mode="json"), "lookback": 5} for c in strategy.conditions],
        }
    )
    with_defaults = sum(1 for c in corpus if engine.evaluate(strategy, c).matched)
    with_five = sum(1 for c in corpus if engine.evaluate(starved, c).matched)
    assert with_defaults > with_five * 5, (
        f"expected the structural default to dominate a 5-bar window "
        f"(defaults={with_defaults}, five={with_five})"
    )


# --- the defaults themselves ----------------------------------------------


@pytest.mark.parametrize(
    ("condition_type", "expected"),
    [
        (ConditionType.MSS, 30),
        (ConditionType.CHOCH, 30),
        (ConditionType.BOS, 30),
        (ConditionType.LIQUIDITY_SWEEP, 30),
        (ConditionType.ORDER_BLOCK, 20),
        (ConditionType.FVG, 5),
        (ConditionType.PREMIUM_DISCOUNT, 5),
    ],
)
def test_lookback_defaults_follow_the_events_formation_time(condition_type, expected):
    assert Condition(type=condition_type).lookback == expected


def test_an_explicit_lookback_is_always_honoured():
    # Including one shorter than the default -- the table is a default, not
    # a floor.
    assert Condition(type=ConditionType.MSS, lookback=2).lookback == 2
    assert Condition(type=ConditionType.FVG, lookback=99).lookback == 99
