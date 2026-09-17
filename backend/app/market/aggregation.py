"""Candle aggregation / timeframe resampling (blueprint §14, §16)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market.timeframes import is_valid_upsample, timeframe_to_minutes
from app.smc.types import Candle


_WEEK_MINUTES = 10080
_DAY_MINUTES = 1440

# Minutes past midnight UTC at which the trading session opens, and the
# anchor for every intraday bucket. 225 = 03:45 UTC = 09:15 IST, the NSE
# open -- the same value, for the same reason, as
# `app.ict.engine.ICTConfig.session_open_utc`, which this used to disagree
# with. A deployment trading anything but NSE has to change it; there is no
# session calendar here to derive it from, and a 24h market wants 0.
#
# Why anchor to the session rather than to midnight UTC. Neither choice
# gives every bar its full period: an NSE session is 375 minutes, which 30,
# 60, 120 and 240 all fail to divide, so exactly one bar a day is short
# whatever we do. The choice is *where that bar sits*, and midnight
# anchoring put it at the open:
#
#     30m  first bar 03:30 UTC (09:00 IST), holding 15 of its 30 minutes
#     1h   first bar 03:00 UTC (08:30 IST), holding 15 of its 60 minutes
#     4h   first bar 00:00 UTC (05:30 IST), holding 15 of its 240 minutes
#
# Each of those is stamped at a time the market had not opened -- 05:30 IST
# is not a period any NSE instrument traded in -- and each buries the
# opening range, the most information-dense part of the day, inside a bar
# that mostly is not trading. Session anchoring moves the short bar to the
# close, where every market already has one and where traders expect it,
# and makes the day's first bar a real first bar.
SESSION_OPEN_MINUTES_UTC = 225


def bucket_start(
    timestamp: datetime,
    target_minutes: int,
    session_open_minutes_utc: int = SESSION_OPEN_MINUTES_UTC,
) -> datetime:
    """The start of the `target_minutes` bucket containing `timestamp`.

    Weekly buckets are anchored on Monday, not on the Unix epoch. Epoch
    arithmetic (`minutes_since_epoch // 10080`) puts the boundary wherever
    1970-01-01 fell, and that was a **Thursday** -- so a "1W" candle ran
    Thursday to Wednesday, opening mid-week and straddling the weekend in
    its middle rather than at its edge. Every other week boundary in this
    codebase is an ISO week (Monday): `app.smc.liquidity.detect_session_levels`
    buckets previous-week highs and lows by `isocalendar()`, and
    `RiskWindow.roll` resets `weekly_pnl` the same way. A weekly candle
    whose open is Thursday's open disagrees with both, and with what a
    weekly bar means to anyone reading it.

    Daily buckets also keep calendar anchoring: a 00:00-24:00 UTC day
    contains the whole NSE session (03:45-10:00), so there is nothing for
    an offset to fix, and Upstox serves "day" bars directly with their own
    stamps.

    **Intraday buckets are anchored to the session open**, not to midnight
    UTC -- see `SESSION_OPEN_MINUTES_UTC` for why, and for what it costs.
    This was carried as an open question for several rounds and is now
    decided. It is worth being exact about the blast radius: production
    derives only 5m and 15m (`app/workers/main.py`) and buckets ticks at
    1m, and 225 divides all of those, so **this changes nothing that runs
    today**. What it closes is the trap waiting for whoever first adds
    "30m" to `derived_timeframes` or resamples to "1h" -- who would
    otherwise get a first bar of the day stamped before the market opened,
    and, if Upstox's own 30-minute bars are session-aligned, a stored
    series in which backfilled and derived bars interleave 15 minutes
    apart instead of coinciding.

    A naive `timestamp` is read as UTC rather than rejected or passed to
    `astimezone()`. Before this, the two branches disagreed about the same
    input: the weekly one has no timezone arithmetic and silently returned
    a bucket, while the sub-weekly one raised `TypeError: can't subtract
    offset-naive and offset-aware datetimes`. Reading it as UTC is the
    convention this codebase already settled twice -- `app.ict.killzones`'s
    `_utc_hour` and `app.market.providers.upstox.parse_candles` both do the
    same, and both explain why `astimezone()` is the wrong call: on a naive
    value it assumes the *machine's* zone, which a UTC-configured CI can
    never catch.
    """
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    if target_minutes == _WEEK_MINUTES:
        return (timestamp - timedelta(days=timestamp.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    minutes_since_epoch = int((timestamp - epoch).total_seconds() // 60)

    # Daily and larger stay on the calendar grid; only intraday sizes take
    # the session offset.
    offset = 0 if target_minutes >= _DAY_MINUTES else session_open_minutes_utc % target_minutes
    bucket_index = (minutes_since_epoch - offset) // target_minutes
    return epoch + timedelta(minutes=bucket_index * target_minutes + offset)


def aggregate_candles(candles: list[Candle]) -> Candle:
    if not candles:
        raise ValueError("Cannot aggregate an empty candle list")
    return Candle(
        timestamp=candles[0].timestamp,
        open=candles[0].open,
        high=max(c.high for c in candles),
        low=min(c.low for c in candles),
        close=candles[-1].close,
        volume=sum(c.volume for c in candles),
    )


def resample_candles(candles: list[Candle], source_timeframe: str, target_timeframe: str) -> list[Candle]:
    """Derives higher-timeframe candles from lower-timeframe ones. Only a
    strictly higher, evenly-dividing target timeframe is supported — the
    engine should never need to fabricate detail that isn't there.

    **The trailing bucket may be incomplete, and nothing here marks it.**
    Eighteen 1m candles resampled to 15m give one 15-minute bar and one
    3-minute bar, and the second is indistinguishable from a closed one:
    `detect_swings`, the strategy engine and every `candles[-1]` read it as
    a finished bar. `CandleWorker` avoids this deliberately -- it only
    aggregates a bucket once `_completes_bucket` says the next base candle
    falls outside it -- and this function has no such knowledge to work
    from, because whether more bars are coming is not something the input
    can say. A market's closed periods make it genuinely undecidable here:
    a weekly bar built from Monday-to-Friday dailies is complete in trading
    terms and short of its wall-clock end.

    So the caller has to know. Where the data is a finished historical
    range the tail is complete and this is a non-issue; where it is a
    range fetched mid-session, the last bar is still forming and will be
    corrected by the next overlapping fetch, since `upsert_candles` is a
    real upsert. Treating that bar as closed in between is the hazard."""
    if not is_valid_upsample(source_timeframe, target_timeframe):
        raise ValueError(f"Cannot derive {target_timeframe} candles from {source_timeframe}")

    target_minutes = timeframe_to_minutes(target_timeframe)
    buckets: dict[datetime, list[Candle]] = {}
    for candle in candles:
        bucket = bucket_start(candle.timestamp, target_minutes)
        buckets.setdefault(bucket, []).append(candle)

    result = []
    for bucket_ts in sorted(buckets):
        group = sorted(buckets[bucket_ts], key=lambda c: c.timestamp)
        aggregated = aggregate_candles(group)
        result.append(
            Candle(
                timestamp=bucket_ts,
                open=aggregated.open,
                high=aggregated.high,
                low=aggregated.low,
                close=aggregated.close,
                volume=aggregated.volume,
            )
        )
    return result
