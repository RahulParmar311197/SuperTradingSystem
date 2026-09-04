"""ICT feature engine — kill zones, opening ranges, and session/liquidity
levels shared with the SMC engine, each individually toggleable (blueprint §26)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

from app.ict.killzones import DEFAULT_KILL_ZONES, KillZone, active_kill_zones
from app.ict.opening_range import OpeningRange, detect_opening_ranges
from app.smc.liquidity import detect_session_levels
from app.smc.types import Candle, LiquidityPool


@dataclass(slots=True)
class ICTConfig:
    enable_kill_zones: bool = True
    enable_session_levels: bool = True
    enable_opening_range: bool = True
    kill_zones: list[KillZone] = field(default_factory=lambda: list(DEFAULT_KILL_ZONES))
    session_open: time = time(9, 15)
    opening_range_minutes: int = 15


@dataclass(slots=True)
class ICTContext:
    current_kill_zones: list[str]
    session_levels: list[LiquidityPool]
    opening_ranges: list[OpeningRange]

    @property
    def current_opening_range(self) -> OpeningRange | None:
        return self.opening_ranges[-1] if self.opening_ranges else None

    def in_kill_zone(self, name: str) -> bool:
        return name in self.current_kill_zones


class ICTEngine:
    def __init__(self, config: ICTConfig | None = None) -> None:
        self.config = config or ICTConfig()

    def analyze(self, candles: list[Candle]) -> ICTContext:
        cfg = self.config

        current_kill_zones: list[str] = []
        if cfg.enable_kill_zones and candles:
            current_kill_zones = active_kill_zones(candles[-1], cfg.kill_zones)

        session_levels: list[LiquidityPool] = []
        if cfg.enable_session_levels:
            session_levels = detect_session_levels(candles, "day") + detect_session_levels(candles, "week")

        opening_ranges: list[OpeningRange] = []
        if cfg.enable_opening_range:
            opening_ranges = detect_opening_ranges(
                candles, cfg.session_open, cfg.opening_range_minutes
            )

        return ICTContext(
            current_kill_zones=current_kill_zones,
            session_levels=session_levels,
            opening_ranges=opening_ranges,
        )
