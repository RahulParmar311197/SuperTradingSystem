"""Candle aggregation / timeframe resampling (blueprint §14, §16)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.market.timeframes import is_valid_upsample, timeframe_to_minutes
from app.smc.types import Candle


_WEEK_MINUTES = 10080


def bucket_start(timestamp: datetime, target_minutes: int) -> datetime:
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

    Sub-weekly buckets keep epoch anchoring. Every one of them divides a
    day evenly, so their boundaries coincide with midnight UTC -- but note
    what that does and does not settle. It does not make them align with a
    *session*: NSE opens at 03:45 UTC, which is a boundary for 3m, 5m and
    15m but lands 15 minutes into a 30m bucket, 45 into an hourly one and
    225 into a 4h one. The first bar of an NSE day at those sizes is
    therefore a stub. That is a deliberate open question about what a
    "1h candle" should mean on this market, recorded in docs/ARCHITECTURE.md
    rather than decided here.

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
    bucket_index = minutes_since_epoch // target_minutes
    return epoch + timedelta(minutes=bucket_index * target_minutes)


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
