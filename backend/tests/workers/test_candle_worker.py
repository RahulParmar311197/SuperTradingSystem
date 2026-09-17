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


async def test_an_incomplete_bucket_does_not_corrupt_the_bucket_before_it(db_instrument):
    # Regression test: `_derive_timeframe` used to pick the base-timeframe
    # candles to aggregate by taking the last `window` rows *positionally*
    # (`recent[-window:]`), assuming the base timeframe has no gaps. If a
    # base candle inside the current target bucket is missing (a dropped
    # tick, a minute in which nothing traded), the positional slice padded
    # the count out with candles from the *previous*, already-derived
    # bucket instead -- and then derived the target timestamp from
    # `recent[0]`, which now belonged to that previous bucket. That
    # silently overwrote the already-correct, already-persisted candle for
    # the previous bucket with data spanning two different periods.
    #
    # Selecting by bucket timestamp rather than by position is what fixes
    # that, and it is what this still guards. What changed since is what
    # happens to the *current* bucket: it used to be dropped, and is now
    # derived from the base candles that exist (see
    # `test_an_incomplete_bucket_is_derived_from_the_minutes_that_traded`).
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m", derived_timeframes=["5m"])
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)  # aligned to a 5-minute boundary

    # First 5m bucket (minutes 0-4): complete, derives normally.
    for i in range(6):
        await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=i), 100.0 + i))

    async with async_session_factory() as db:
        five_min = await get_candles(db, db_instrument.id, "5m")
    assert len(five_min) == 1
    assert five_min[0].open == 100.0
    assert five_min[0].close == 104.0

    # Second 5m bucket (minutes 5-9): minute 7's tick never arrives, so
    # this bucket only ever has 4 of its 5 base candles. The bucket still
    # "completes" by wall-clock boundary (minute 9's candle closes when
    # minute 10's tick arrives), so derivation still fires.
    for i in (6, 8, 9, 10):
        await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=i), 100.0 + i))

    async with async_session_factory() as db:
        five_min = await get_candles(db, db_instrument.id, "5m")

    # The first bucket's candle is untouched -- not overwritten with data
    # spanning both periods, and not re-timestamped.
    assert len(five_min) == 2
    assert five_min[0].timestamp == start
    assert five_min[0].open == 100.0
    assert five_min[0].close == 104.0

    # And the second bucket is its own bar, at its own timestamp, built
    # from minutes 5, 6, 8 and 9 -- prices 105, 106, 108, 109.
    assert five_min[1].timestamp == start + timedelta(minutes=5)
    assert five_min[1].open == 105.0
    assert five_min[1].high == 109.0
    assert five_min[1].low == 105.0
    assert five_min[1].close == 109.0


async def test_an_incomplete_bucket_is_derived_from_the_minutes_that_traded(db_instrument):
    """A minute with no trades contributes nothing to an OHLCV bar, so the
    aggregate of the minutes that did trade *is* the bar.

    This used to write nothing at all. The reasoning was that a partial
    bucket might misrepresent its period; measured, the opposite held. On
    a bucket whose middle minute never traded, the aggregate of the
    remaining base candles came out byte-identical to the bar computed
    from the raw ticks -- the open is the period's first traded price, the
    high and low its extremes, the close its last, the volume its sum, and
    an untraded minute supplies none of them.

    Dropping the bar was also not inert. Nothing downstream reads
    timestamps for adjacency: `detect_swings` and the indexed SMC
    detectors walk the list positionally, so a missing bar welds two
    non-adjacent periods together. Measured over a 300-bar series,
    dropping any single bar changed `detect_swings` output in 82 of 290
    positions, with real swings vanishing and swings appearing that never
    happened.

    Asserted against the ticks that were actually fed, not against
    hand-copied constants, so the bar has to match the period it claims
    to describe rather than merely be non-empty.
    """
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m", derived_timeframes=["5m"])
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)

    # Minutes 0-4 form the first bucket; minute 2 never trades. Minute 5's
    # tick is what closes minute 4's candle and completes the bucket, so
    # it is fed but belongs to the *next* bucket.
    traded_minutes = (0, 1, 3, 4)
    prices = {m: 100.0 + m for m in traded_minutes}
    for i in (*traded_minutes, 5):
        await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=i), 100.0 + i))

    async with async_session_factory() as db:
        five_min = await get_candles(db, db_instrument.id, "5m")

    assert len(five_min) == 1, "the bar for a bucket with an untraded minute must still be written"
    bar = five_min[0]
    assert bar.timestamp == start
    assert bar.open == prices[min(traded_minutes)]
    assert bar.high == max(prices.values())
    assert bar.low == min(prices.values())
    assert bar.close == prices[max(traded_minutes)]
    # `_tick` carries volume 10 and each minute here gets exactly one tick,
    # so the bar's volume is the traded minutes' and nothing else: the
    # untraded minute must not be counted as a minute of zero volume that
    # somehow drags anything, nor must a neighbouring bucket's tick leak in.
    assert bar.volume == 10.0 * len(traded_minutes)


async def test_an_incomplete_bucket_is_visible_to_an_operator(db_instrument, caplog):
    """Writing the bar does not make the incompleteness uninteresting.

    A bar built from fewer minutes than the period has is either an
    instrument that barely trades or a feed losing ticks, and from inside
    this worker the two are indistinguishable. In the second case the bar
    understates the period's range and volume, so an operator has to be
    able to see it happening -- climbing on one instrument is the signal.
    """
    import logging

    from app.core.metrics import DERIVED_CANDLE_INCOMPLETE

    def _incomplete() -> float:
        return DERIVED_CANDLE_INCOMPLETE.labels("5m")._value.get()

    before = _incomplete()
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m", derived_timeframes=["5m"])
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)

    with caplog.at_level(logging.WARNING, logger="workers.candle"):
        for i in (0, 1, 3, 4, 5):
            await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=i), 100.0 + i))

    assert _incomplete() == before + 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("4 of 5 base candles" in m for m in warnings), warnings
    # The message has to say the bar was written, not that it was skipped:
    # an operator who reads "not deriving" will go looking for a hole that
    # is not there.
    assert any("The bar is written" in m for m in warnings), warnings


async def test_a_complete_bucket_logs_nothing_and_counts_nothing(db_instrument, caplog):
    # Control: the warning must fire on the gap and on nothing else, or it
    # is noise an operator will learn to ignore.
    import logging

    from app.core.metrics import DERIVED_CANDLE_INCOMPLETE

    before = DERIVED_CANDLE_INCOMPLETE.labels("5m")._value.get()
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m", derived_timeframes=["5m"])
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)

    with caplog.at_level(logging.WARNING, logger="workers.candle"):
        for i in range(6):
            await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=i), 100.0 + i))

    async with async_session_factory() as db:
        assert len(await get_candles(db, db_instrument.id, "5m")) == 1
    assert DERIVED_CANDLE_INCOMPLETE.labels("5m")._value.get() == before
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_a_bucket_with_nothing_traded_in_it_writes_no_bar(db_instrument):
    """Control, and the one case where writing nothing is still right.

    A bar has to have an open, and an open is a traded price; a period in
    which nothing traded at all has none, and `aggregate_candles` raises
    on an empty list rather than inventing one. This branch is defensive
    rather than reachable through `process_tick` -- that path always calls
    `_derive_timeframe` just after committing a base candle that lies
    inside the bucket being derived -- so it is exercised directly, and
    labelled as defensive rather than dressed up as a scenario.
    """
    worker = CandleWorker({db_instrument.symbol: db_instrument.id}, base_timeframe="1m", derived_timeframes=["5m"])
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)

    for i in range(6):
        await worker.process_tick(_tick(db_instrument.symbol, start + timedelta(minutes=i), 100.0 + i))

    async with async_session_factory() as db:
        before = await get_candles(db, db_instrument.id, "5m")
    assert len(before) == 1

    # A bucket an hour later, into which no base candle was ever written.
    await worker._derive_timeframe(
        db_instrument.id, "5m", 5, start + timedelta(hours=1, minutes=4)
    )

    async with async_session_factory() as db:
        after = await get_candles(db, db_instrument.id, "5m")
    assert [c.timestamp for c in after] == [c.timestamp for c in before], (
        "an empty bucket must write no bar, and must not disturb the bars around it"
    )
