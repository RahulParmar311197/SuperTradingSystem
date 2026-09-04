"""Fair Value Gap detection and mitigation tracking (blueprint §23)."""

from __future__ import annotations

from app.smc.types import Candle, FairValueGap, FVGDirection


def detect_fvgs(
    candles: list[Candle], min_gap_pct: float = 0.0, timeframe: str = ""
) -> list[FairValueGap]:
    """Classic 3-candle imbalance: candle[i-2], candle[i-1] (displacement),
    candle[i]. A bullish FVG exists when candle[i-2].high < candle[i].low;
    a bearish FVG when candle[i-2].low > candle[i].high.
    """
    gaps: list[FairValueGap] = []

    for i in range(2, len(candles)):
        c0, c2 = candles[i - 2], candles[i]

        if c0.high < c2.low:
            size_pct = (c2.low - c0.high) / c0.high * 100 if c0.high else 0
            if size_pct >= min_gap_pct:
                gaps.append(
                    FairValueGap(
                        direction=FVGDirection.BULLISH,
                        top=c2.low,
                        bottom=c0.high,
                        created_index=i,
                        created_at=candles[i - 1].timestamp,
                        timeframe=timeframe,
                    )
                )

        if c0.low > c2.high:
            size_pct = (c0.low - c2.high) / c2.high * 100 if c2.high else 0
            if size_pct >= min_gap_pct:
                gaps.append(
                    FairValueGap(
                        direction=FVGDirection.BEARISH,
                        top=c0.low,
                        bottom=c2.high,
                        created_index=i,
                        created_at=candles[i - 1].timestamp,
                        timeframe=timeframe,
                    )
                )

    update_mitigation(candles, gaps)
    return gaps


def update_mitigation(candles: list[Candle], gaps: list[FairValueGap]) -> None:
    """Recomputes fill/mitigation state for each gap using candles after it
    was created. Mutates the gap objects in place."""
    for gap in gaps:
        deepest_fill = 0.0
        for i in range(gap.created_index + 1, len(candles)):
            candle = candles[i]
            overlap_high = min(candle.high, gap.top)
            overlap_low = max(candle.low, gap.bottom)
            if overlap_high <= overlap_low:
                continue

            if gap.direction == FVGDirection.BULLISH:
                fill_from_bottom = gap.top - overlap_low
            else:
                fill_from_bottom = overlap_high - gap.bottom
            deepest_fill = max(deepest_fill, fill_from_bottom)

            if candle.low <= gap.bottom and candle.high >= gap.top:
                gap.invalidated = True

        gap.filled_percentage = min(deepest_fill / gap.size, 1.0) if gap.size else 0.0
        gap.mitigated = gap.filled_percentage >= 1.0 or gap.invalidated
