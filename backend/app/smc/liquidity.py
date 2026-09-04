"""Liquidity pool detection and sweep/rejection tracking (blueprint §22)."""

from __future__ import annotations

from app.smc.types import (
    Candle,
    LiquidityPool,
    LiquiditySide,
    LiquiditySourceType,
    Swing,
    SwingType,
)


def detect_equal_levels(swings: list[Swing], tolerance_pct: float = 0.05) -> list[LiquidityPool]:
    """Group nearby swing highs into equal-highs pools (buy-side liquidity)
    and nearby swing lows into equal-lows pools (sell-side liquidity)."""
    pools: list[LiquidityPool] = []

    for swing_type, side, source in (
        (SwingType.HIGH, LiquiditySide.BUY_SIDE, LiquiditySourceType.EQUAL_HIGHS),
        (SwingType.LOW, LiquiditySide.SELL_SIDE, LiquiditySourceType.EQUAL_LOWS),
    ):
        candidates = sorted(
            (s for s in swings if s.swing_type == swing_type), key=lambda s: s.price
        )
        used: set[int] = set()
        for anchor in candidates:
            if anchor.index in used:
                continue
            tolerance = anchor.price * (tolerance_pct / 100)
            group = [
                s
                for s in candidates
                if s.index not in used and abs(s.price - anchor.price) <= tolerance
            ]
            if len(group) < 2:
                continue
            for s in group:
                used.add(s.index)
            avg_price = sum(s.price for s in group) / len(group)
            first = min(group, key=lambda s: s.index)
            pools.append(
                LiquidityPool(
                    side=side,
                    source_type=source,
                    price=avg_price,
                    formed_index=first.index,
                    formed_timestamp=first.timestamp,
                    member_indices=[s.index for s in group],
                )
            )

    return pools


def detect_session_levels(
    candles: list[Candle], period: str = "day"
) -> list[LiquidityPool]:
    """Previous-day / previous-week high & low liquidity levels (blueprint §22).

    `period` is "day" or "week". Each detected level is anchored at the
    first candle timestamp of the *following* period, since that is when
    the level becomes a resting liquidity target rather than an in-progress
    extreme.
    """
    if period not in ("day", "week"):
        raise ValueError("period must be 'day' or 'week'")

    def bucket_key(ts):
        return ts.date() if period == "day" else (ts.isocalendar().year, ts.isocalendar().week)

    high_source = (
        LiquiditySourceType.PREVIOUS_DAY_HIGH if period == "day" else LiquiditySourceType.PREVIOUS_WEEK_HIGH
    )
    low_source = (
        LiquiditySourceType.PREVIOUS_DAY_LOW if period == "day" else LiquiditySourceType.PREVIOUS_WEEK_LOW
    )

    pools: list[LiquidityPool] = []
    if not candles:
        return pools

    current_key = bucket_key(candles[0].timestamp)
    period_high = candles[0].high
    period_low = candles[0].low

    for i, candle in enumerate(candles):
        key = bucket_key(candle.timestamp)
        if key != current_key:
            pools.append(
                LiquidityPool(
                    side=LiquiditySide.BUY_SIDE,
                    source_type=high_source,
                    price=period_high,
                    formed_index=i,
                    formed_timestamp=candle.timestamp,
                )
            )
            pools.append(
                LiquidityPool(
                    side=LiquiditySide.SELL_SIDE,
                    source_type=low_source,
                    price=period_low,
                    formed_index=i,
                    formed_timestamp=candle.timestamp,
                )
            )
            current_key = key
            period_high = candle.high
            period_low = candle.low
        else:
            period_high = max(period_high, candle.high)
            period_low = min(period_low, candle.low)

    return pools


def detect_sweeps(candles: list[Candle], pools: list[LiquidityPool]) -> None:
    """Mutates `pools` in place, marking sweeps and rejections.

    A sweep occurs when price trades through the pool level after it was
    formed; a rejection additionally requires the candle to close back on
    the origin side, which is the classic "stop hunt then reverse" pattern.
    """
    for pool in pools:
        for i in range(pool.formed_index + 1, len(candles)):
            candle = candles[i]
            if pool.side == LiquiditySide.BUY_SIDE and candle.high > pool.price:
                pool.swept = True
                pool.swept_index = i
                pool.swept_timestamp = candle.timestamp
                pool.rejected = candle.close < pool.price
                break
            if pool.side == LiquiditySide.SELL_SIDE and candle.low < pool.price:
                pool.swept = True
                pool.swept_index = i
                pool.swept_timestamp = candle.timestamp
                pool.rejected = candle.close > pool.price
                break
