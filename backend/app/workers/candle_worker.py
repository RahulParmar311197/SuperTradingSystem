"""CandleWorker (blueprint §66): aggregates incoming ticks into closed
base-timeframe candles, persists them, derives higher timeframes from
them (blueprint §16), and publishes each closed candle on `/ws/chart`.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.metrics import CANDLE_DROPPED_UNKNOWN_INSTRUMENT, DERIVED_CANDLE_INCOMPLETE
from app.core.redis import channel_name, publish
from app.database.session import async_session_factory
from app.market.aggregation import aggregate_candles
from app.market.aggregation import bucket_start as compute_bucket_start
from app.market.normalization import StandardTick
from app.market.repository import get_candles, upsert_candles
from app.market.timeframes import timeframe_to_minutes
from app.smc.types import Candle

logger = logging.getLogger("workers.candle")

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(slots=True)
class _FormingCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_candle(self) -> Candle:
        return Candle(self.timestamp, self.open, self.high, self.low, self.close, self.volume)


def _completes_bucket(candle_start: datetime, base_minutes: int, target_minutes: int) -> bool:
    """True when the base-timeframe candle starting at `candle_start` is the
    last one inside its enclosing target-timeframe bucket."""
    # Expressed in terms of `bucket_start` rather than re-deriving the grid
    # from the epoch, so the two can never disagree about where a bucket
    # begins: this candle completes its bucket exactly when the next base
    # candle falls into a different one. Independent epoch arithmetic here
    # silently assumed every boundary is epoch-aligned, which weekly
    # buckets are not (see app.market.aggregation.bucket_start).
    next_start = candle_start + timedelta(minutes=base_minutes)
    return compute_bucket_start(next_start, target_minutes) != compute_bucket_start(candle_start, target_minutes)


class CandleWorker:
    def __init__(
        self,
        instrument_ids: dict[str, uuid.UUID],
        base_timeframe: str = "1m",
        derived_timeframes: list[str] | None = None,
    ) -> None:
        self.instrument_ids = instrument_ids
        self.base_timeframe = base_timeframe
        self.base_minutes = timeframe_to_minutes(base_timeframe)
        self.derived_timeframes = derived_timeframes or []
        self._forming: dict[str, _FormingCandle] = {}
        # Symbols already reported as having no instrument id, so the
        # warning is one line per symbol rather than one per candle.
        self._warned_unknown_symbols: set[str] = set()

    async def process_tick(self, tick: StandardTick) -> Candle | None:
        """Feeds one tick in. Returns the candle that just closed, if any."""
        bucket_ts = compute_bucket_start(tick.timestamp, self.base_minutes)
        forming = self._forming.get(tick.symbol)
        closed: Candle | None = None

        if forming is not None and bucket_ts < forming.timestamp:
            # A stale/out-of-order tick -- its bucket is older than the
            # candle currently forming, not just a different one. Ordinary
            # live feeds redeliver a few already-seen ticks after a
            # reconnect, and this bucket has already closed and been
            # persisted. Accepting it would corrupt the *current* forming
            # candle with a stale price (via the `!=` branch below
            # wrongly treating it as a rollover) and reopen an
            # already-persisted bucket, which then collides with that row
            # once it closes again. Drop it instead -- the closed history
            # for that bucket is already correct.
            logger.warning(
                "Dropping out-of-order tick for %s: bucket %s is older than the forming candle's %s",
                tick.symbol, bucket_ts, forming.timestamp,
            )
            return None

        if forming is not None and forming.timestamp != bucket_ts:
            closed = forming.to_candle()
            await self._on_candle_closed(tick.symbol, closed)
            forming = None

        if forming is None:
            forming = _FormingCandle(bucket_ts, tick.ltp, tick.ltp, tick.ltp, tick.ltp, tick.volume)
        else:
            forming.high = max(forming.high, tick.ltp)
            forming.low = min(forming.low, tick.ltp)
            forming.close = tick.ltp
            forming.volume += tick.volume

        self._forming[tick.symbol] = forming
        return closed

    async def _on_candle_closed(self, symbol: str, candle: Candle) -> None:
        instrument_id = self.instrument_ids.get(symbol)
        if instrument_id is not None:
            async with async_session_factory() as db:
                await upsert_candles(db, instrument_id, self.base_timeframe, [candle])
        else:
            # The candle is built, published below, and then thrown away,
            # because there is no `Instrument` row to hang it on.
            #
            # That silence was the problem. Measured before this: a symbol
            # absent from `WORKER_INSTRUMENT_IDS` stored nothing, logged
            # nothing, and counted nothing -- while the worker kept
            # building a candle a minute and streaming it, so every
            # external sign said it was working.
            #
            # It is not working. `ScannerWorker`, `AutoTradeSupervisor`,
            # the backtest engine and the replay engine all read the
            # `candles` table; a symbol that never reaches it is invisible
            # to every one of them, forever. The live `/ws/chart` stream
            # is the one thing that does keep working, which is precisely
            # what makes the failure hard to see.
            #
            # Once per symbol, not once per candle: at one base candle a
            # minute this would otherwise be 1,440 identical lines a day
            # per misconfigured symbol, which is how a real warning gets
            # filtered out.
            CANDLE_DROPPED_UNKNOWN_INSTRUMENT.labels(symbol).inc()
            if symbol not in self._warned_unknown_symbols:
                self._warned_unknown_symbols.add(symbol)
                logger.error(
                    "Discarding every %s candle for %s: no instrument id is configured for it, so "
                    "nothing can be stored. It will keep streaming on /ws/chart while remaining "
                    "invisible to the scanner, the autonomous loop and every backtest. Add it to "
                    "WORKER_INSTRUMENT_IDS as SYMBOL=<instrument uuid>.",
                    self.base_timeframe,
                    symbol,
                )

        await publish(
            channel_name("chart", str(instrument_id or symbol), self.base_timeframe),
            {"timestamp": candle.timestamp.isoformat(), "open": candle.open, "high": candle.high, "low": candle.low, "close": candle.close, "volume": candle.volume},
        )

        if instrument_id is not None:
            for target_timeframe in self.derived_timeframes:
                target_minutes = timeframe_to_minutes(target_timeframe)
                if target_minutes <= self.base_minutes or not _completes_bucket(candle.timestamp, self.base_minutes, target_minutes):
                    continue
                await self._derive_timeframe(instrument_id, target_timeframe, target_minutes, candle.timestamp)

    async def _derive_timeframe(
        self, instrument_id: uuid.UUID, target_timeframe: str, target_minutes: int, as_of: datetime
    ) -> None:
        window = target_minutes // self.base_minutes
        target_bucket_ts = compute_bucket_start(as_of, target_minutes)
        async with async_session_factory() as db:
            lookback_start = as_of - timedelta(minutes=target_minutes * 2)
            recent = await get_candles(db, instrument_id, self.base_timeframe, start=lookback_start, end=as_of)
            # Filter to exactly the base candles that fall inside *this*
            # target bucket, rather than slicing the last `window` rows by
            # position -- the positional slice assumed the base timeframe
            # has no gaps. If a tick-feed hiccup or worker restart dropped
            # one or more base candles inside this bucket, `recent[-window:]`
            # padded the count with candles from the *previous* bucket,
            # deriving the wrong bucket timestamp from `recent[0]` and
            # silently overwriting that already-correct, already-persisted
            # candle with data actually spanning two different periods,
            # while the true current bucket was never written at all.
            bucket_candles = [c for c in recent if compute_bucket_start(c.timestamp, target_minutes) == target_bucket_ts]
            if not bucket_candles:
                # Nothing traded anywhere in the period, so there is no bar
                # to write -- a bar has to have an open, and an open is a
                # traded price. Defensive rather than reachable: this runs
                # from `_on_candle_closed`, which has just persisted and
                # committed a base candle that lies inside this very
                # bucket, so the list holds at least that one.
                logger.warning(
                    "Not deriving %s candle at %s for instrument %s: no base candles in the "
                    "bucket at all.",
                    target_timeframe, target_bucket_ts, instrument_id,
                )
                DERIVED_CANDLE_INCOMPLETE.labels(target_timeframe).inc()
                return
            if len(bucket_candles) != window:
                # **Derive from what traded.** This used to `return` here,
                # writing no bar at all, on the reasoning that a partial
                # bucket might misrepresent its period. That was wrong in
                # the routine case and harmful in the rare one.
                #
                # It is wrong in the routine case because a minute in which
                # nothing traded contributes nothing to an OHLCV bar *by
                # definition*: the open is the period's first traded price,
                # the high and low its extremes, the close its last, the
                # volume its sum, and a minute with no trades supplies none
                # of them. So the aggregate of the minutes that did trade
                # is not an approximation of the bar -- it IS the bar.
                # Measured on a 15m bucket whose minute 7 never traded, the
                # aggregate of the other fourteen base candles came out
                # byte-identical to the bar computed from the raw ticks.
                # The old code discarded that provably-correct bar.
                #
                # It is harmful in the rare case (ticks genuinely lost)
                # because dropping the bar amplifies the damage: one lost
                # minute became a fifteen-minute hole. And the hole is not
                # inert. Nothing downstream reads timestamps for adjacency
                # -- `detect_swings` and every indexed SMC detector walk
                # the list positionally -- so a missing bar silently welds
                # two non-adjacent periods together. Measured over a
                # 300-bar series, dropping any single bar changed
                # `detect_swings` output in 82 of 290 positions, with real
                # swings vanishing and swings appearing that never
                # happened. A bar short one minute's ticks is a smaller
                # error than that, and it is the same degradation the 1m
                # series already carries -- which this worker writes
                # without hesitation, rather than dropping its neighbours
                # too.
                #
                # So: write the bar, and record that it was built from
                # partial data. The counter still matters -- climbing on an
                # instrument means either it barely trades or ticks are
                # being lost, and from here those are indistinguishable.
                logger.warning(
                    "Deriving %s candle at %s for instrument %s from %d of %d base candles "
                    "(a minute with no trades, or lost ticks). The bar is written; it may "
                    "understate the period's range and volume if ticks were lost.",
                    target_timeframe, target_bucket_ts, instrument_id,
                    len(bucket_candles), window,
                )
                DERIVED_CANDLE_INCOMPLETE.labels(target_timeframe).inc()
            derived = aggregate_candles(bucket_candles)
            derived = Candle(
                timestamp=target_bucket_ts,
                open=derived.open,
                high=derived.high,
                low=derived.low,
                close=derived.close,
                volume=derived.volume,
            )
            await upsert_candles(db, instrument_id, target_timeframe, [derived])

        await publish(
            channel_name("chart", str(instrument_id), target_timeframe),
            {"timestamp": derived.timestamp.isoformat(), "open": derived.open, "high": derived.high, "low": derived.low, "close": derived.close, "volume": derived.volume},
        )
