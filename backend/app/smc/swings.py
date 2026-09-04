"""Swing high/low detection (blueprint §19).

A candle at index i is a swing high if its high is the strictest maximum
among the `swing_length` candles on each side; a swing low is the mirror
case on lows. A swing is only *confirmed* once `swing_length` candles exist
after it — until then we simply don't know if it holds, so it must not be
reported. This confirmation delay is what keeps the detector look-ahead
safe: callers only ever hand us `candles[:t+1]`, so a swing near the tail of
that slice is correctly left undetected until later data arrives.
"""

from __future__ import annotations

from app.smc.types import Candle, Swing, SwingLabel, SwingType


def detect_swings(candles: list[Candle], swing_length: int = 3) -> list[Swing]:
    if swing_length < 1:
        raise ValueError("swing_length must be >= 1")

    swings: list[Swing] = []
    n = len(candles)

    for i in range(swing_length, n - swing_length):
        window_highs = [candles[j].high for j in range(i - swing_length, i + swing_length + 1)]
        window_lows = [candles[j].low for j in range(i - swing_length, i + swing_length + 1)]
        pivot_high = candles[i].high
        pivot_low = candles[i].low

        if pivot_high == max(window_highs) and window_highs.count(pivot_high) == 1:
            swings.append(
                Swing(
                    index=i,
                    confirmed_index=i + swing_length,
                    timestamp=candles[i].timestamp,
                    price=pivot_high,
                    swing_type=SwingType.HIGH,
                )
            )

        if pivot_low == min(window_lows) and window_lows.count(pivot_low) == 1:
            swings.append(
                Swing(
                    index=i,
                    confirmed_index=i + swing_length,
                    timestamp=candles[i].timestamp,
                    price=pivot_low,
                    swing_type=SwingType.LOW,
                )
            )

    swings.sort(key=lambda s: s.index)
    _label_swings(swings)
    return swings


def _label_swings(swings: list[Swing]) -> None:
    last_high: Swing | None = None
    last_low: Swing | None = None

    for swing in swings:
        if swing.swing_type == SwingType.HIGH:
            if last_high is None:
                swing.label = SwingLabel.NONE
            else:
                swing.label = SwingLabel.HH if swing.price > last_high.price else SwingLabel.LH
            last_high = swing
        else:
            if last_low is None:
                swing.label = SwingLabel.NONE
            else:
                swing.label = SwingLabel.HL if swing.price > last_low.price else SwingLabel.LL
            last_low = swing


def visible_swings(swings: list[Swing], as_of_index: int) -> list[Swing]:
    """Swings confirmed at or before `as_of_index` — safe to use for decisions made at that index."""
    return [s for s in swings if s.confirmed_index <= as_of_index]
