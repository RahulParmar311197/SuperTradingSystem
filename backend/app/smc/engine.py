"""SMC engine facade — composes swings, structure, liquidity, FVG, order
blocks and premium/discount into one `SMCContext` (blueprint §18, §30).

Callers (scanner, strategy engine, replay, backtest, AI prompt builder) only
need this one entry point. Pass a candle list truncated to the current
timestamp and the result is guaranteed look-ahead safe (blueprint §45),
since every detector below only ever looks inside the list it is given.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.smc.fvg import detect_fvgs
from app.smc.liquidity import detect_equal_levels, detect_session_levels, detect_sweeps
from app.smc.order_blocks import detect_order_blocks
from app.smc.premium_discount import dealing_range_from_swings
from app.smc.structure import detect_structure_events, promote_confirmed_shifts
from app.smc.swings import detect_swings
from app.smc.types import (
    Candle,
    FairValueGap,
    LiquidityPool,
    OrderBlock,
    PremiumDiscountZone,
    StructureEvent,
    Swing,
)


@dataclass(slots=True)
class SMCConfig:
    swing_length: int = 3
    equal_level_tolerance_pct: float = 0.05
    fvg_min_gap_pct: float = 0.0
    order_block_lookback: int = 10
    detect_daily_levels: bool = True
    detect_weekly_levels: bool = True


@dataclass(slots=True)
class SMCContext:
    swings: list[Swing]
    structure_events: list[StructureEvent]
    mss_events: list[StructureEvent]
    liquidity_pools: list[LiquidityPool]
    fair_value_gaps: list[FairValueGap]
    order_blocks: list[OrderBlock]
    dealing_range: PremiumDiscountZone | None
    current_price: float | None
    current_zone: str | None = field(default=None)

    @property
    def bias(self) -> str | None:
        if not self.structure_events:
            return None
        return self.structure_events[-1].direction.value

    def unmitigated_fvgs(self, direction: str | None = None) -> list[FairValueGap]:
        return [
            g
            for g in self.fair_value_gaps
            if not g.mitigated and (direction is None or g.direction.value == direction)
        ]

    def active_order_blocks(self, direction: str | None = None) -> list[OrderBlock]:
        return [
            b
            for b in self.order_blocks
            if not b.mitigated and (direction is None or b.direction.value == direction)
        ]

    def recent_sweeps(self, side: str | None = None) -> list[LiquidityPool]:
        return [
            p
            for p in self.liquidity_pools
            if p.swept and (side is None or p.side.value == side)
        ]


class SMCEngine:
    def __init__(self, config: SMCConfig | None = None) -> None:
        self.config = config or SMCConfig()

    def analyze(self, candles: list[Candle]) -> SMCContext:
        cfg = self.config
        swings = detect_swings(candles, swing_length=cfg.swing_length)
        structure_events = detect_structure_events(candles, swings)
        mss_events = promote_confirmed_shifts(structure_events)

        pools = detect_equal_levels(swings, tolerance_pct=cfg.equal_level_tolerance_pct)
        if cfg.detect_daily_levels:
            pools += detect_session_levels(candles, period="day")
        if cfg.detect_weekly_levels:
            pools += detect_session_levels(candles, period="week")
        detect_sweeps(candles, pools)

        fvgs = detect_fvgs(candles, min_gap_pct=cfg.fvg_min_gap_pct)
        order_blocks = detect_order_blocks(
            candles, structure_events, fvgs, lookback_candles=cfg.order_block_lookback
        )
        dealing_range = dealing_range_from_swings(swings)

        current_price = candles[-1].close if candles else None
        current_zone = dealing_range.zone_for(current_price) if dealing_range and current_price is not None else None

        return SMCContext(
            swings=swings,
            structure_events=structure_events,
            mss_events=mss_events,
            liquidity_pools=pools,
            fair_value_gaps=fvgs,
            order_blocks=order_blocks,
            dealing_range=dealing_range,
            current_price=current_price,
            current_zone=current_zone,
        )
