from datetime import datetime, timedelta, timezone

from app.market.normalization import StandardTick
from app.market.repository import get_candles
from app.database.session import async_session_factory
from app.workers.candle_worker import CandleWorker, _completes_bucket


def _tick(symbol: str, ts: datetime, ltp: float) -> StandardTick:
    return StandardTick(symbol=symbol, exchange="NSE", market="EQUITY", timestamp=ts, ltp=ltp, volume=10)


def test_completes_bucket_logic():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # a 1m candle at :00 does not complete a 5m bucket, but the one at :04 does
    assert _completes_bucket(start, base_minutes=1, target_minutes=5) is False
    assert _completes_bucket(start + timedelta(minutes=4), base_minutes=1, target_minutes=5) is True


async def test_process_tick_forms_and_closes_candles(db_instrument):
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m")
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)

    closed = await worker.process_tick(_tick(db_instrument.symbol, start, 100.0))
    assert closed is None  # first tick just opens the forming candle

    await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(seconds=20), 105.0))
    closed = await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=1), 102.0))

    assert closed is not None
    assert closed.open == 100.0
    assert closed.high == 105.0
    assert closed.close == 105.0  # last price before the new bucket started

    async with async_session_factory() as db:
        persisted = await get_candles(db, db_instrument.id, "1m")
    assert len(persisted) == 1
    assert persisted[0].open == 100.0


async def test_process_tick_drops_a_stale_out_of_order_tick(db_instrument):
    # Regression test: `process_tick` only checked `forming.timestamp !=
    # bucket_ts` to decide whether to roll over to a new candle -- it
    # never checked the new bucket was chronologically *after* the
    # forming one. A stale/re-delivered tick (ordinary after a live feed
    # reconnect) whose bucket is *older* than the currently forming
    # candle got treated exactly like a legitimate rollover: the current,
    # correct, still-accumulating candle was prematurely closed with
    # whatever partial data it had, and a bogus new forming candle opened
    # at the old, already-closed bucket -- which then collided with that
    # bucket's already-persisted row (uq_candle_key) the next time it
    # closed.
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m")
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)

    await worker.process_tick(_tick(db_instrument.symbol, start, 100.0))
    closed = await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=1), 102.0))
    assert closed is not None  # the 09:15 candle closed normally, opening 09:16

    # A stale tick whose bucket (09:15) is older than the candle
    # currently forming (09:16) -- must be dropped, not treated as a
    # rollover.
    stale = await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(seconds=30), 999.0))
    assert stale is None

    # The 09:16 candle must be untouched by the stale tick's price, and
    # must still be the one forming when the next real tick arrives.
    closed = await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=2), 103.0))
    assert closed is not None
    assert closed.timestamp == start + timedelta(minutes=1)
    assert closed.high == 102.0  # never touched by the stale 999.0 tick
    assert closed.low == 102.0

    async with async_session_factory() as db:
        persisted = await get_candles(db, db_instrument.id, "1m")
    # Exactly the two genuine candles (09:15, 09:16) -- no bogus
    # duplicate/second row for 09:15 from the stale tick, and no crash
    # from re-persisting it.
    assert len(persisted) == 2
    assert [c.timestamp for c in persisted] == [start, start + timedelta(minutes=1)]


async def test_derived_timeframe_is_persisted_after_enough_base_candles(db_instrument):
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m", derived_timeframes=["5m"])
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)  # aligned to a 5-minute boundary

    # Feed 6 minutes of ticks (one per minute) so the first 5m bucket closes.
    for i in range(6):
        await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=i), 100.0 + i))

    async with async_session_factory() as db:
        five_min = await get_candles(db, db_instrument.id, "5m")

    assert len(five_min) == 1
    assert five_min[0].open == 100.0
