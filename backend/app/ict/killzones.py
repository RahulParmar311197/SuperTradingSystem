"""ICT kill zones — configurable UTC time-of-day windows (blueprint §26)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

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


def _utc_hour(timestamp: datetime) -> float:
    """The timestamp's hour-of-day **in UTC**, which is what `KillZone`
    windows are declared in.

    This used to read `timestamp.hour` directly, which is the hour in
    whatever zone the timestamp happens to carry. The same instant then
    produced different zones depending only on how the caller spelled it:
    `2026-01-05T03:45:00+00:00` gave `['ASIAN']` and the *equal*
    `2026-01-05T09:15:00+05:30` gave `['LONDON']`. That is the natural
    spelling for an NSE client -- `POST /paper/{id}/candle` takes the
    timestamp straight from the request body -- and three of four sampled
    NSE session times came out in the wrong zone (09:15 IST reported
    LONDON when it is ASIAN; 13:00 reported NEW_YORK when it is LONDON).
    `ConditionType.SESSION` gates strategy matching on these, so a
    London-only strategy fired during the Asian session.

    A naive timestamp is read as UTC explicitly rather than passed to
    `astimezone()`, which would assume the *machine's* local zone --
    the same class of bug one layer down, and one a UTC-configured CI
    could never catch. Candles from Postgres are always aware (the column
    is `TIMESTAMP WITH TIME ZONE`); naive ones come from fixtures and
    in-memory construction, where UTC is the convention this codebase
    already uses.
    """
    if timestamp.tzinfo is None:
        utc = timestamp.replace(tzinfo=timezone.utc)
    else:
        utc = timestamp.astimezone(timezone.utc)
    return utc.hour + utc.minute / 60


def active_kill_zones(candle: Candle, zones: list[KillZone] | None = None) -> list[str]:
    zones = zones if zones is not None else DEFAULT_KILL_ZONES
    hour = _utc_hour(candle.timestamp)
    return [z.name for z in zones if z.contains(hour)]
