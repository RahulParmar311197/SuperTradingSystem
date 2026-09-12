"""Ready-to-run strategies shipped with the platform (blueprint §34).

Every strategy here is covered by `tests/strategy/test_library.py`, which
constructs price action containing the setup it names and asserts it
actually produces a signal. That is the whole point of this module: before
it existed, the only strategy the blueprint showed -- §34's "Bullish
Liquidity Sweep" -- appeared nowhere in the codebase, was never run, and
in fact could not fire, because the DSL's flat 5-bar `lookback` default
expired the sweep before the structure shift it caused could confirm (see
`_DEFAULT_LOOKBACK_BY_TYPE` in app/strategy/dsl.py).

These are starting points, not recommendations. None has been validated on
real market data, and `POST /backtest/validate` exists precisely so a
strategy can be tested out-of-sample before it is trusted with money.
"""

from __future__ import annotations

from app.strategy.dsl import StrategyDefinition

# Blueprint §34, reproduced exactly as the blueprint prints it -- including
# omitting `lookback`, so this doubles as the standing check that the
# document's own example works out of the box.
BULLISH_LIQUIDITY_SWEEP: dict = {
    "name": "Bullish Liquidity Sweep",
    "market": "NIFTY",
    "timeframe": "15m",
    "direction": "bullish",
    "conditions": [
        {"type": "liquidity_sweep", "side": "sell"},
        {"type": "mss", "direction": "bullish"},
        {"type": "fvg", "direction": "bullish"},
    ],
    "entry": {"type": "fvg_retest"},
    "risk": {"risk_percent": 0.5, "minimum_rr": 2},
}

# The same idea inverted: buy-side liquidity taken out above the highs,
# structure shifts down, and the imbalance left behind on the way down is
# where the short is entered.
BEARISH_LIQUIDITY_SWEEP: dict = {
    "name": "Bearish Liquidity Sweep",
    "market": "NIFTY",
    "timeframe": "15m",
    "direction": "bearish",
    "conditions": [
        {"type": "liquidity_sweep", "side": "buy"},
        {"type": "mss", "direction": "bearish"},
        {"type": "fvg", "direction": "bearish"},
    ],
    "entry": {"type": "fvg_retest"},
    "risk": {"risk_percent": 0.5, "minimum_rr": 2},
}

# Continuation rather than reversal: structure has already broken upward,
# and the order block that caused the break is traded on the pullback into
# it. No sweep is required, so this fires far more often than the two
# above -- the trade-off is that it takes no view on whether the move is
# exhausted.
BULLISH_ORDER_BLOCK_RETEST: dict = {
    "name": "Bullish Order Block Retest",
    "market": "NIFTY",
    "timeframe": "15m",
    "direction": "bullish",
    "conditions": [
        {"type": "bos", "direction": "bullish"},
        {"type": "order_block", "direction": "bullish"},
    ],
    "entry": {"type": "order_block_retest"},
    "risk": {"risk_percent": 0.5, "minimum_rr": 2},
}

BEARISH_ORDER_BLOCK_RETEST: dict = {
    "name": "Bearish Order Block Retest",
    "market": "NIFTY",
    "timeframe": "15m",
    "direction": "bearish",
    "conditions": [
        {"type": "bos", "direction": "bearish"},
        {"type": "order_block", "direction": "bearish"},
    ],
    "entry": {"type": "order_block_retest"},
    "risk": {"risk_percent": 0.5, "minimum_rr": 2},
}

# Only buy imbalance while price is in the lower half of the dealing range
# (blueprint §26 premium/discount). The `premium_discount` condition is
# what keeps this from chasing strength into the top of the range.
DISCOUNT_FVG_LONG: dict = {
    "name": "Discount FVG Long",
    "market": "NIFTY",
    "timeframe": "15m",
    "direction": "bullish",
    "conditions": [
        {"type": "premium_discount", "zone": "discount"},
        {"type": "fvg", "direction": "bullish"},
    ],
    "entry": {"type": "fvg_retest"},
    "risk": {"risk_percent": 0.5, "minimum_rr": 2},
}

LIBRARY: dict[str, dict] = {
    "bullish_liquidity_sweep": BULLISH_LIQUIDITY_SWEEP,
    "bearish_liquidity_sweep": BEARISH_LIQUIDITY_SWEEP,
    "bullish_order_block_retest": BULLISH_ORDER_BLOCK_RETEST,
    "bearish_order_block_retest": BEARISH_ORDER_BLOCK_RETEST,
    "discount_fvg_long": DISCOUNT_FVG_LONG,
}


def load(key: str) -> StrategyDefinition:
    """The named library strategy, validated through the DSL.

    Validating here rather than storing pre-built objects means a library
    entry that stops satisfying the schema -- a condition type later
    rejected as unfed, an entry type the engine cannot resolve -- fails
    loudly at import or on first use, instead of shipping a strategy that
    silently never matches.
    """
    if key not in LIBRARY:
        raise KeyError(f"Unknown library strategy {key!r}. Available: {', '.join(sorted(LIBRARY))}")
    return StrategyDefinition.model_validate(LIBRARY[key])


def load_all() -> dict[str, StrategyDefinition]:
    return {key: load(key) for key in LIBRARY}
