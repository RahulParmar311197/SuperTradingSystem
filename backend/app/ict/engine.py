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
    # **UTC**, like `KillZone`'s window bounds, and named so that is
    # visible at every use site. `detect_opening_ranges` compares this
    # clock time against the candle's own, so it has to be expressed in
    # the zone the candles carry -- and every candle in this system comes
    # from Postgres, where the column is `TIMESTAMP WITH TIME ZONE` and
    # values arrive in UTC.
    #
    # The default used to be `time(9, 15)`: the NSE open, but written in
    # IST and handed to UTC data. Measured on one NSE day stamped in UTC,
    # that anchored the "opening range" at 09:15 UTC = 14:45 IST -- the
    # middle of the afternoon -- reporting ordinary mid-session bars and
    # missing the real opening fifteen minutes entirely. No caller
    # overrides this field, so every opening range the system produced was
    # the wrong bars.
    #
    # 03:45 UTC *is* 09:15 IST. A deployment trading anything but NSE has
    # to set this; there is no session calendar here to derive it from.
    session_open_utc: time = time(3, 45)
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
                candles, cfg.session_open_utc, cfg.opening_range_minutes
            )

        return ICTContext(
            current_kill_zones=current_kill_zones,
            session_levels=session_levels,
            opening_ranges=opening_ranges,
        )
