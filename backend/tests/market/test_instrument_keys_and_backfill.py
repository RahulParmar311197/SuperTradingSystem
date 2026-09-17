"""Symbol -> provider key, and the backfill that uses it.

`Instrument.symbol` is a trading symbol ("INFY", "NIFTY"); Upstox names
instruments as "NSE_EQ|INE009A01021" or "NSE_INDEX|Nifty 50" and accepts
nothing else. Nothing in this codebase translated between the two, so no
provider call could name an instrument at all.

The two things most worth pinning here are both silent failures rather
than loud ones: an index whose master entry is title-cased ("Nifty 50")
never matching an upper-cased symbol, and a timeframe fetched at one
interval but stored under another's label — which would store fine, run
fine, and make every backtest number wrong.
"""

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.session import async_session_factory
from app.market.backfill import backfill_candles, upstox_interval_for
from app.market.providers.instrument_master import (
    InstrumentKeyUnknown,
    apply_instrument_keys,
    parse_master_records,
    resolve_instrument_key,
)
from app.smc.types import Candle

# Shaped as Upstox publishes it: equities upper-cased, indices title-cased.
MASTER_RECORDS = [
    {
        "segment": "NSE_EQ",
        "exchange": "NSE",
        "trading_symbol": "INFY",
        "instrument_key": "NSE_EQ|INE009A01021",
        "instrument_type": "EQ",
    },
    {
        "segment": "NSE_INDEX",
        "exchange": "NSE",
        "trading_symbol": "Nifty 50",
        "instrument_key": "NSE_INDEX|Nifty 50",
        "instrument_type": "INDEX",
    },
    {"segment": "NSE_EQ", "exchange": "NSE", "trading_symbol": "", "instrument_key": "NSE_EQ|BROKEN"},
    {"segment": "NSE_EQ", "exchange": "NSE", "trading_symbol": "NOKEY", "instrument_key": ""},
]


class _StubMarketData:
    """Stands in for `UpstoxMarketData`. Records what it was asked for —
    the mismatched-interval bug is only visible in the request."""

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls: list[tuple] = []

    async def get_historical_candles(self, instrument_key, interval, from_date, to_date):
        self.calls.append((instrument_key, interval, from_date, to_date))
        return self.candles


# --- parsing the master ---------------------------------------------------


def test_an_equity_is_indexed_by_exchange_and_symbol():
    index = parse_master_records(MASTER_RECORDS)
    assert index[("NSE", "INFY")] == "NSE_EQ|INE009A01021"


def test_a_title_cased_index_entry_is_still_found_by_an_upper_cased_symbol():
    # The one that matters on an NSE platform: Upstox writes "Nifty 50",
    # a symbol column holds "NIFTY 50". Exact matching misses every index.
    index = parse_master_records(MASTER_RECORDS)
    assert index[("NSE", "NIFTY 50")] == "NSE_INDEX|Nifty 50"


@pytest.mark.parametrize("bad", [{}, {"instrument_key": "K"}, {"trading_symbol": "S"}, "not-a-dict"])
def test_a_record_missing_what_it_needs_is_skipped_not_stored_half_formed(bad):
    assert parse_master_records([bad]) == {}


def test_the_good_records_survive_the_bad_ones():
    # Control for the skipping above: two of the four fixtures are junk.
    assert len(parse_master_records(MASTER_RECORDS)) == 2


# --- resolving at the point of use ----------------------------------------


def test_a_missing_key_names_the_instrument_and_the_remedy():
    instrument = Instrument(symbol="NIFTY", exchange="NSE", market=MarketType.EQUITY, instrument_type="INDEX")
    with pytest.raises(InstrumentKeyUnknown) as excinfo:
        resolve_instrument_key(instrument)
    assert "NIFTY" in str(excinfo.value)
    assert "apply_instrument_keys" in str(excinfo.value)


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_a_blank_key_is_as_absent_as_a_missing_one(blank):
    instrument = Instrument(symbol="X", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
    instrument.broker_instrument_key = blank
    with pytest.raises(InstrumentKeyUnknown):
        resolve_instrument_key(instrument)


def test_a_stored_key_is_returned():
    instrument = Instrument(symbol="INFY", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
    instrument.broker_instrument_key = "NSE_EQ|INE009A01021"
    assert resolve_instrument_key(instrument) == "NSE_EQ|INE009A01021"


# --- the interval mapping -------------------------------------------------


@pytest.mark.parametrize(("timeframe", "interval"), [("1m", "1minute"), ("30m", "30minute"), ("1D", "day")])
def test_a_directly_served_timeframe_maps_to_its_provider_interval(timeframe, interval):
    assert upstox_interval_for(timeframe) == interval


@pytest.mark.parametrize("timeframe", ["5m", "15m", "1h", "4h"])
def test_a_timeframe_upstox_cannot_serve_is_refused_rather_than_approximated(timeframe):
    # The silent one. Fetching 1minute bars and storing them as "15m"
    # would store fine, backtest fine, and be wrong in every number.
    with pytest.raises(ValueError, match="does not serve"):
        upstox_interval_for(timeframe)


def test_an_unknown_timeframe_is_refused_too():
    with pytest.raises(ValueError, match="not a supported timeframe"):
        upstox_interval_for("7s")


# --- the backfill ---------------------------------------------------------


async def _instrument(db, *, key: str | None) -> Instrument:
    instrument = Instrument(
        symbol=f"BF{uuid.uuid4().hex[:6].upper()}",
        exchange="NSE",
        market=MarketType.EQUITY,
        instrument_type="EQ",
    )
    instrument.broker_instrument_key = key
    db.add(instrument)
    await db.commit()
    await db.refresh(instrument)
    return instrument


async def test_a_backfill_stores_the_candles_it_fetched(require_infra):
    candles = [
        Candle(datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc), 100.0, 101.0, 99.5, 100.5, 1000),
        Candle(datetime(2026, 9, 16, 3, 46, tzinfo=timezone.utc), 100.5, 102.0, 100.0, 101.5, 1200),
    ]
    async with async_session_factory() as db:
        instrument = await _instrument(db, key="NSE_EQ|INE009A01021")
        stub = _StubMarketData(candles)
        try:
            result = await backfill_candles(
                db, stub, instrument, "1m", date(2026, 9, 16), date(2026, 9, 16)
            )
            assert result.candles_written == 2
            assert result.first_timestamp == candles[0].timestamp
            assert result.last_timestamp == candles[-1].timestamp

            # It asked for the resolved key and the mapped interval, not
            # the plain symbol and not the raw timeframe.
            assert stub.calls == [("NSE_EQ|INE009A01021", "1minute", date(2026, 9, 16), date(2026, 9, 16))]

            rows = (
                await db.execute(select(CandleRow).where(CandleRow.instrument_id == instrument.id))
            ).scalars().all()
            assert len(rows) == 2
            assert {r.timeframe for r in rows} == {"1m"}
        finally:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument.id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument.id))
            await db.commit()


async def test_running_the_same_backfill_twice_is_idempotent(require_infra):
    # `upsert_candles` is a real upsert; a re-run over an overlapping range
    # must not raise on `uq_candle_key` or double the rows.
    candles = [Candle(datetime(2026, 9, 16, 3, 45, tzinfo=timezone.utc), 100.0, 101.0, 99.5, 100.5, 1000)]
    async with async_session_factory() as db:
        instrument = await _instrument(db, key="NSE_EQ|X")
        stub = _StubMarketData(candles)
        try:
            await backfill_candles(db, stub, instrument, "1m", date(2026, 9, 16), date(2026, 9, 16))
            await backfill_candles(db, stub, instrument, "1m", date(2026, 9, 16), date(2026, 9, 16))
            rows = (
                await db.execute(select(CandleRow).where(CandleRow.instrument_id == instrument.id))
            ).scalars().all()
            assert len(rows) == 1
        finally:
            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument.id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument.id))
            await db.commit()


async def test_a_backfill_without_a_key_fails_before_it_calls_the_provider(require_infra):
    async with async_session_factory() as db:
        instrument = await _instrument(db, key=None)
        stub = _StubMarketData([])
        try:
            with pytest.raises(InstrumentKeyUnknown):
                await backfill_candles(db, stub, instrument, "1m", date(2026, 9, 16), date(2026, 9, 16))
            # Never asked: a request under a symbol the provider does not
            # know is a wasted call and an opaque error.
            assert stub.calls == []
        finally:
            await db.execute(delete(Instrument).where(Instrument.id == instrument.id))
            await db.commit()


# --- applying a master to the instrument table ----------------------------


async def test_applying_a_master_fills_what_it_can_and_reports_what_it_cannot(require_infra):
    async with async_session_factory() as db:
        known = Instrument(symbol="INFY", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        unknown = Instrument(
            symbol=f"NOTINMASTER{uuid.uuid4().hex[:4].upper()}",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="EQ",
        )
        db.add_all([known, unknown])
        await db.commit()
        ids = [known.id, unknown.id]
        try:
            report = await apply_instrument_keys(db, parse_master_records(MASTER_RECORDS))
            assert report.matched["INFY"] == "NSE_EQ|INE009A01021"
            # The point of the report type: a partial load must be visible,
            # not discovered later as a backfill that returns nothing.
            assert unknown.symbol in report.unmatched
            assert report.complete is False

            await db.refresh(known)
            assert known.broker_instrument_key == "NSE_EQ|INE009A01021"
        finally:
            await db.execute(delete(Instrument).where(Instrument.id.in_(ids)))
            await db.commit()


async def test_a_second_load_does_not_overwrite_a_stored_key_by_default(require_infra):
    # A key already there was loaded before or corrected by hand; re-running
    # a master load should not quietly replace it.
    async with async_session_factory() as db:
        instrument = Instrument(symbol="INFY", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        instrument.broker_instrument_key = "NSE_EQ|HAND-CORRECTED"
        db.add(instrument)
        await db.commit()
        try:
            report = await apply_instrument_keys(db, parse_master_records(MASTER_RECORDS))
            assert "INFY" in report.unchanged
            await db.refresh(instrument)
            assert instrument.broker_instrument_key == "NSE_EQ|HAND-CORRECTED"

            # Control: overwrite=True is how you actually re-seed.
            report = await apply_instrument_keys(db, parse_master_records(MASTER_RECORDS), overwrite=True)
            await db.refresh(instrument)
            assert instrument.broker_instrument_key == "NSE_EQ|INE009A01021"
            assert report.matched["INFY"] == "NSE_EQ|INE009A01021"
        finally:
            await db.execute(delete(Instrument).where(Instrument.id == instrument.id))
            await db.commit()
