"""ICT kill zones — configurable UTC time-of-day windows (blueprint §26)."""

from __future__ import annotations

from dataclasses import dataclass

from app.smc.types import Candle


@dataclass(slots=True)
class KillZone:
    name: str
    start_hour_utc: float
    end_hour_utc: float

    def contains(self, hour_utc: float) -> bool:
        if self.start_hour_utc <= self.end_hour_utc:
            return self.start_hour_utc <= hour_utc < self.end_hour_utc
        return hour_utc >= self.start_hour_utc or hour_utc < self.end_hour_utc  # wraps past midnight


DEFAULT_KILL_ZONES: list[KillZone] = [
    KillZone("ASIAN", 0.0, 4.0),
    KillZone("LONDON", 7.0, 10.0),
    KillZone("NEW_YORK", 12.0, 15.0),
    KillZone("LONDON_CLOSE", 15.0, 16.0),
]


def active_kill_zones(candle: Candle, zones: list[KillZone] | None = None) -> list[str]:
    zones = zones if zones is not None else DEFAULT_KILL_ZONES
    hour = candle.timestamp.hour + candle.timestamp.minute / 60
    return [z.name for z in zones if z.contains(hour)]
