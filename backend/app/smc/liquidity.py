"""Liquidity pool detection and sweep/rejection tracking (blueprint §22)."""

from __future__ import annotations

import bisect

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
        # `candidates` is sorted by price and the grouping test is a band
        # around the anchor's price, so every member of a group is
        # contiguous here. Scanning the whole list per anchor made this
        # O(swings^2), and swings grow with history. Binary search bounds
        # the scan to the band instead.
        prices = [s.price for s in candidates]
        used: set[int] = set()
        for anchor in candidates:
            if anchor.index in used:
                continue
            tolerance = anchor.price * (tolerance_pct / 100)
            # One extra position each side, then the original predicate
            # unchanged: the band bounds are recomputed floats, so a value
            # exactly on the boundary could otherwise land a single ulp
            # outside the slice. The predicate, not the slice, decides
            # membership.
            lo = max(0, bisect.bisect_left(prices, anchor.price - tolerance) - 1)
            hi = min(len(candidates), bisect.bisect_right(prices, anchor.price + tolerance) + 1)
            group = [
                s
                for s in candidates[lo:hi]
                if s.index not in used and abs(s.price - anchor.price) <= tolerance
            ]
            if len(group) < 2:
                continue
            for s in group:
                used.add(s.index)
            avg_price = sum(s.price for s in group) / len(group)
            # Anchor the pool at its *last* member: an equal-highs/lows pool
            # does not exist until the swing that makes the level "equal" has
            # printed. Anchoring at the first member left `detect_sweeps`
            # scanning a window that still contained the pool's own later
            # members, and since `price` is the group average, any member
            # priced beyond that average swept the pool on its own candle --
            # reporting a liquidity grab at (or before) the moment the pool
            # formed, when price had never traded through the level. Because
            # `detect_sweeps` breaks on the first hit, that phantom sweep also
            # pinned `swept_index` and hid the genuine sweep that came later.
            # `detect_session_levels` below already gets this right, anchoring
            # each level at the first candle of the *following* period.
            last = max(group, key=lambda s: s.index)
            pools.append(
                LiquidityPool(
                    side=side,
                    source_type=source,
                    price=avg_price,
                    formed_index=last.index,
                    formed_timestamp=last.timestamp,
                    member_indices=sorted(s.index for s in group),
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

    The scan starts *at* `formed_index` and excludes only the pool's own
    constituent swings. The invariant being enforced is "a pool cannot be
    swept by the very swings that define it", and `member_indices` states
    that directly; `formed_index + 1` was a positional stand-in for it that
    only happens to coincide for one of the two pool kinds.

    For an equal-highs/lows pool `formed_index` *is* the last member, so it
    is skipped either way and nothing changes. A previous-day/week level is
    the opposite case: `detect_session_levels` anchors it at the first candle
    of the *following* period -- the bar at which the level becomes a resting
    liquidity target, not a bar that helped form it, since `period_high` and
    `period_low` are reset only after the pool is emitted. Skipping that bar
    dropped the single candle most likely to raid the prior session's
    extreme. A judas swing that takes out the previous-day high on the
    opening bar and closes back below it -- the platform's headline setup --
    reported `swept=False`, and where a later bar also traded through the
    level the sweep was attributed to that bar instead, at its smaller
    magnitude.
    """
    for pool in pools:
        members = set(pool.member_indices)
        for i in range(pool.formed_index, len(candles)):
            if i in members:
                continue
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
