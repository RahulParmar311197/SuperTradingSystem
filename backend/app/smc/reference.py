"""An independent second opinion on our SMC detectors.

This wraps the `smartmoneyconcepts` package (MIT, github.com/joshyattridge)
so its detectors can be run against the same candles as `app.smc` and the
two compared. It exists to catch regressions in our own engine: an
independent implementation agreeing with a detector is evidence the
detector still works, and a new disagreement is a prompt to go and look.

**This must never be used on the live, replay or backtest path**, and not
because of taste -- it was measured. Blueprint §45 makes look-ahead
prevention mandatory, and the library does not provide it:

* `smc.fvg` decides row `i` using `.shift(-1)`, i.e. candle `i + 1`. Feeding
  it `candles[:k]` and then `candles[:k + 1]` changed its verdict on the
  last already-visible bar on **37 of 249** bar arrivals. `app.smc.fvg`
  changed on **0 of 249**.
* `smc.swing_highs_lows` post-filters swings into a strictly alternating
  HIGH/LOW sequence, which is recomputed globally on every call. Over the
  same 249 arrivals it **retracted 253 swings** it had previously
  reported. `app.smc.swings` retracted **0**, and every swing it added was
  the one bar that had just become confirmable -- which is exactly what
  `Swing.confirmed_index` models.

A retraction means a level reported to a live consumer later ceased to
exist. That is fine for offline analysis and disqualifying for anything
that places an order.

The two engines also differ by design, not by defect, and the comparison
tests encode which differences are understood:

* **FVG.** The library additionally requires the middle candle to close in
  the gap's direction (`& (ohlc["close"] > ohlc["open"])`); we apply the
  plain three-candle imbalance. So the library finds strictly fewer gaps,
  and every gap it finds is one we find too -- an invariant the tests
  assert rather than assume.
* **Swings.** The alternation filter above discards intermediate pivots of
  the same type. Where both engines report a swing on the same bar they
  have never disagreed about whether it is a HIGH or a LOW.
* **Index convention.** The library anchors a gap at the middle candle;
  `FairValueGap.created_index` anchors at the third, the first bar on
  which the gap is knowable. `reference_fvgs` normalises to ours.

The library is a development dependency (`requirements-dev.txt`), imported
lazily inside each function so that neither it nor numba is pulled into
the production image or the application's import graph.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.smc.types import Candle, FVGDirection, SwingType

_MISSING = (
    "smartmoneyconcepts is not installed. It is a development dependency used "
    "only to cross-check app.smc against an independent implementation: "
    "pip install -r requirements-dev.txt"
)


@dataclass(frozen=True, slots=True)
class ReferenceSwing:
    """A swing as the library sees it, in our index convention."""

    index: int
    price: float
    swing_type: SwingType


@dataclass(frozen=True, slots=True)
class ReferenceGap:
    """A fair value gap as the library sees it, in our index convention.

    `created_index` has already been shifted to match
    `FairValueGap.created_index` -- the library's own row number is one
    lower.
    """

    direction: FVGDirection
    top: float
    bottom: float
    created_index: int


def _frame(candles: list[Candle]):
    import pandas as pd

    return pd.DataFrame(
        {
            "open": [c.open for c in candles],
            "high": [c.high for c in candles],
            "low": [c.low for c in candles],
            "close": [c.close for c in candles],
            "volume": [c.volume for c in candles],
        }
    )


def _smc():
    try:
        from smartmoneyconcepts import smc
    except ImportError as exc:  # pragma: no cover - import guard
        raise ImportError(_MISSING) from exc
    return smc


def reference_swings(candles: list[Candle], swing_length: int = 3) -> list[ReferenceSwing]:
    """The library's swings, normalised to our conventions.

    Index 0 is dropped. The library emits it on every series tested, but a
    pivot needs `swing_length` bars on each side and bar 0 has none to its
    left, so it is a boundary artefact rather than a detection.
    """
    if not candles:
        return []

    import pandas as pd

    frame = _smc().swing_highs_lows(_frame(candles), swing_length=swing_length)
    swings: list[ReferenceSwing] = []
    for row_index, row in frame.iterrows():
        if pd.isna(row["HighLow"]) or int(row_index) == 0:
            continue
        swings.append(
            ReferenceSwing(
                index=int(row_index),
                price=float(row["Level"]),
                swing_type=SwingType.HIGH if int(row["HighLow"]) == 1 else SwingType.LOW,
            )
        )
    return swings


def reference_fvgs(candles: list[Candle]) -> list[ReferenceGap]:
    """The library's fair value gaps, re-anchored to our index convention.

    The library reports a gap on the middle candle of the three; we anchor
    it on the third, which is the first bar on which the gap can be known.
    The `+ 1` here is that re-anchoring, not a fudge factor.
    """
    if not candles:
        return []

    import pandas as pd

    frame = _smc().fvg(_frame(candles))
    gaps: list[ReferenceGap] = []
    for row_index, row in frame.iterrows():
        if pd.isna(row["FVG"]):
            continue
        gaps.append(
            ReferenceGap(
                direction=FVGDirection.BULLISH if int(row["FVG"]) == 1 else FVGDirection.BEARISH,
                top=float(row["Top"]),
                bottom=float(row["Bottom"]),
                created_index=int(row_index) + 1,
            )
        )
    return gaps
