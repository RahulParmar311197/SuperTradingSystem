"""Filling the candle store from a market-data provider (blueprint §14).

This is what turns the seeded random walks the strategy library is tested
against into real price action. Everything downstream -- the backtest
engine, the out-of-sample validation, the paper engine's own candle feed --
reads `candles` from Postgres and does not care where a bar came from, so
this module is the only place that has to know a provider exists.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.instruments import Instrument
from app.market.providers.instrument_master import resolve_instrument_key
from app.market.repository import upsert_candles
from app.market.timeframes import SUPPORTED_TIMEFRAMES
from app.smc.types import Candle

logger = logging.getLogger("market.backfill")

# Only the timeframes Upstox serves directly. Everything else in
# `SUPPORTED_TIMEFRAMES` is *derived* from 1m by `resample_candles`, and
# fetching one interval while labelling the rows as another would be
# invisible: the candles would store, the backtest would run, and every
# number it produced would be wrong. So the mapping is explicit and the
# caller passes this codebase's timeframe, never the provider's interval —
# there is no way to hand in a mismatched pair.
_UPSTOX_INTERVAL_BY_TIMEFRAME: dict[str, str] = {
    "1m": "1minute",
    "30m": "30minute",
    "1D": "day",
    "1W": "week",
}


@dataclass(slots=True)
class BackfillResult:
    symbol: str
    timeframe: str
    candles_written: int
    first_timestamp: object | None = None
    last_timestamp: object | None = None


def upstox_interval_for(timeframe: str) -> str:
    """The provider interval for one of this codebase's timeframes."""
    if timeframe not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"{timeframe!r} is not a supported timeframe: {SUPPORTED_TIMEFRAMES}")
    interval = _UPSTOX_INTERVAL_BY_TIMEFRAME.get(timeframe)
    if interval is None:
        raise ValueError(
            f"Upstox does not serve {timeframe!r} directly (it offers "
            f"{sorted(_UPSTOX_INTERVAL_BY_TIMEFRAME)}). Backfill '1m' and derive this one with "
            "app.market.aggregation.resample_candles -- reading its note on the trailing "
            "bucket first -- rather than storing another interval's bars under this label."
        )
    return interval


async def backfill_candles(
    db: AsyncSession,
    market_data,
    instrument: Instrument,
    timeframe: str,
    from_date: date,
    to_date: date,
) -> BackfillResult:
    """Fetch `instrument`'s history over the range and store it.

    `market_data` is anything with `get_historical_candles` — the read-only
    `UpstoxMarketData` in production, a stub in tests. Typed loosely on
    purpose: this module must not import a client that holds a credential
    just to name its type.

    Writes go through `upsert_candles`, which is a real upsert, so running
    a backfill twice over an overlapping range is idempotent rather than a
    unique-constraint crash.
    """
    key = resolve_instrument_key(instrument)
    interval = upstox_interval_for(timeframe)
    candles: list[Candle] = await market_data.get_historical_candles(key, interval, from_date, to_date)
    await upsert_candles(db, instrument.id, timeframe, candles)
    logger.info(
        "Backfilled %d %s candles for %s (%s) from %s to %s",
        len(candles),
        timeframe,
        instrument.symbol,
        key,
        from_date,
        to_date,
    )
    return BackfillResult(
        symbol=instrument.symbol,
        timeframe=timeframe,
        candles_written=len(candles),
        first_timestamp=candles[0].timestamp if candles else None,
        last_timestamp=candles[-1].timestamp if candles else None,
    )
