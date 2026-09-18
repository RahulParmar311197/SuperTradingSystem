"""`option_snapshots` had three readers and no writer.

`POST /options/execute` reads it for all three of its options-specific
gates — liquidity, premium deviation against the real mid, quote staleness
— and `app/trading/portfolio_snapshots.py` reads it to mark option
positions to market. Nothing in `app/` inserted a row, so an earlier round
had to make the endpoint record `None` for each check rather than let the
audit row claim a check that never ran. Honest, and inert.

This covers the writer, and the reader bug the writer would otherwise have
triggered: both consumers resolved the instrument's `OptionContract` with
`.scalar_one_or_none()`, which raises `MultipleResultsFound` as soon as
there is more than one — which is what the *second* chain fetch produces.
Measured against Postgres before the fix:

    two contracts for one instrument -> MultipleResultsFound
"""

import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.options import OptionChainSnapshot, OptionContract, OptionSnapshot
from app.database.session import async_session_factory
from app.market.providers.upstox import parse_option_chain
from app.options.ingestion import ChainSpotPriceUnavailable, ingest_option_chain
from app.options.snapshots import latest_option_snapshot

# No module-level `pytest.mark.asyncio`: half of these are pure parsing
# tests, and pytest.ini sets `asyncio_mode = auto`, so marking the module
# only warns on the sync ones.


# A chain payload shaped like Upstox v2's documented `/option/chain`
# response. Not confirmed against live servers -- this environment cannot
# reach them -- which is exactly why parsing is a pure function with a
# fixture, correctable from one real call later (blueprint §120).
def _chain_payload(call_key: str, put_key: str) -> dict:
    return {
        "status": "success",
        "data": [
            {
                "expiry": "2026-12-31",
                "strike_price": 25000.0,
                "underlying_spot_price": 25123.45,
                "call_options": {
                    "instrument_key": call_key,
                    "market_data": {
                        "ltp": 101.5,
                        "volume": 12345,
                        "oi": 98765,
                        "bid_price": 101.0,
                        "ask_price": 102.0,
                    },
                    "option_greeks": {"iv": 14.2, "delta": 0.55, "gamma": 0.001, "theta": -8.4, "vega": 12.1},
                },
                "put_options": {
                    "instrument_key": put_key,
                    "market_data": {"ltp": 88.0, "volume": 4321, "oi": 5555, "bid_price": 87.5, "ask_price": 88.5},
                    "option_greeks": {"iv": 15.1, "delta": -0.45, "gamma": 0.001, "theta": -7.9, "vega": 11.8},
                },
            }
        ],
    }


class _StubChainProvider:
    """Stands in for `UpstoxMarketData`, which cannot be reached here."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[tuple[str, date]] = []

    async def get_option_chain(self, instrument_key: str, expiry: date):
        self.calls.append((instrument_key, expiry))
        return parse_option_chain(self.payload)


# --- parsing the provider's shape ----------------------------------------


def test_both_sides_of_a_strike_are_parsed():
    """Behavioural proof. A chain row carries a call and a put; dropping
    either halves the contracts anything downstream can ever gate on."""
    chain = parse_option_chain(_chain_payload("NSE_FO|1111", "NSE_FO|2222"))

    assert chain.underlying_spot_price == 25123.45
    by_type = {q.option_type: q for q in chain.quotes}
    assert set(by_type) == {"CE", "PE"}
    assert by_type["CE"].instrument_key == "NSE_FO|1111"
    assert by_type["CE"].bid == 101.0 and by_type["CE"].ask == 102.0
    assert by_type["CE"].volume == 12345 and by_type["CE"].open_interest == 98765
    assert by_type["CE"].delta == 0.55
    assert by_type["PE"].strike == 25000.0


def test_a_row_with_no_strike_is_skipped_rather_than_stored_at_zero():
    """Behavioural proof. Strike 0.0 is not a neutral placeholder -- it is
    a real strike, deep in the money for every underlying, and the
    liquidity and premium gates would read it as one."""
    payload = _chain_payload("NSE_FO|1111", "NSE_FO|2222")
    payload["data"].append({"underlying_spot_price": 25123.45, "call_options": {"instrument_key": "NSE_FO|9999"}})

    chain = parse_option_chain(payload)

    assert [q.instrument_key for q in chain.quotes] == ["NSE_FO|1111", "NSE_FO|2222"]
    assert all(q.strike == 25000.0 for q in chain.quotes)


@pytest.mark.parametrize("bad", [None, "", "NA"], ids=["null", "empty", "NA"])
def test_an_unusable_price_becomes_none_not_a_crash(bad):
    """Behavioural proof. `float("")` raises, and one bad strike must not
    discard the chain around it."""
    payload = _chain_payload("NSE_FO|1111", "NSE_FO|2222")
    payload["data"][0]["call_options"]["market_data"]["bid_price"] = bad

    chain = parse_option_chain(payload)

    call = next(q for q in chain.quotes if q.option_type == "CE")
    assert call.bid is None
    assert call.ask == 102.0, "the rest of the quote must survive"


def test_a_contract_that_has_not_traded_reads_as_zero_volume_not_unknown():
    """Control, and a real distinction. `ltp` absent means "no last trade";
    `volume` absent means the count is zero, which is precisely what makes
    `evaluate_liquidity` reject a dead strike instead of skipping it."""
    payload = _chain_payload("NSE_FO|1111", "NSE_FO|2222")
    payload["data"][0]["call_options"]["market_data"] = {"bid_price": 1.0, "ask_price": 2.0}

    call = next(q for q in parse_option_chain(payload).quotes if q.option_type == "CE")

    assert call.ltp is None
    assert call.volume == 0.0 and call.open_interest == 0.0


def test_an_empty_payload_parses_to_an_empty_chain():
    """Control. The tests above would all pass against a parser that
    returned everything; this pins that it returns nothing when there is
    nothing, rather than inventing a strike."""
    chain = parse_option_chain({"status": "success", "data": []})
    assert chain.quotes == [] and chain.underlying_spot_price is None


# --- writing it into the store -------------------------------------------


async def _make_underlying_and_contracts(prefix: str) -> tuple[Instrument, Instrument, Instrument]:
    expiry = date.today() + timedelta(days=7)
    async with async_session_factory() as db:
        underlying = Instrument(
            symbol=f"{prefix}IDX",
            exchange="NSE",
            market=MarketType.EQUITY,
            instrument_type="INDEX",
            broker_instrument_key=f"NSE_INDEX|{prefix}",
            lot_size=1,
        )
        call = Instrument(
            symbol=f"{prefix}25000CE",
            exchange="NSE",
            market=MarketType.OPTIONS,
            instrument_type="OPTION",
            underlying=f"{prefix}IDX",
            expiry=expiry,
            strike=25000.0,
            option_type=OptionType.CALL,
            broker_instrument_key=f"NSE_FO|{prefix}CE",
            lot_size=50,
        )
        put = Instrument(
            symbol=f"{prefix}25000PE",
            exchange="NSE",
            market=MarketType.OPTIONS,
            instrument_type="OPTION",
            underlying=f"{prefix}IDX",
            expiry=expiry,
            strike=25000.0,
            option_type=OptionType.PUT,
            broker_instrument_key=f"NSE_FO|{prefix}PE",
            lot_size=50,
        )
        db.add_all([underlying, call, put])
        await db.commit()
        for row in (underlying, call, put):
            await db.refresh(row)
        return underlying, call, put


async def _cleanup_instruments(*instruments: Instrument) -> None:
    """Tear down by *chain*, not by instrument.

    A chain these instruments appear in may also carry contracts for other
    instruments, and deleting the chain while one of those still points at
    it is a foreign-key violation -- which is a teardown that fails on a
    test whose assertions passed, i.e. a misleading red. So: find the
    chains, then delete every contract under them.
    """
    async with async_session_factory() as db:
        ids = [i.id for i in instruments]
        chain_ids = set(
            (
                await db.execute(select(OptionContract.chain_id).where(OptionContract.instrument_id.in_(ids)))
            ).scalars().all()
        )
        if chain_ids:
            contract_ids = (
                await db.execute(select(OptionContract.id).where(OptionContract.chain_id.in_(chain_ids)))
            ).scalars().all()
            if contract_ids:
                await db.execute(delete(OptionSnapshot).where(OptionSnapshot.option_contract_id.in_(contract_ids)))
                await db.execute(delete(OptionContract).where(OptionContract.id.in_(contract_ids)))
            await db.execute(delete(OptionChainSnapshot).where(OptionChainSnapshot.id.in_(chain_ids)))
        await db.execute(delete(Instrument).where(Instrument.id.in_(ids)))
        await db.commit()


async def test_ingestion_writes_a_snapshot_for_every_registered_contract(require_infra):
    """Behavioural proof, and the whole point of the round: after this runs
    the store holds real quotes that the execution gates can read."""
    prefix = f"ING{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    provider = _StubChainProvider(_chain_payload(call.broker_instrument_key, put.broker_instrument_key))
    try:
        async with async_session_factory() as db:
            result = await ingest_option_chain(db, provider, underlying, call.expiry)
            await db.commit()

        assert provider.calls == [(underlying.broker_instrument_key, call.expiry)]
        assert result.snapshots_written == 2
        assert result.unregistered_count == 0
        assert result.spot_price == 25123.45

        async with async_session_factory() as db:
            snapshot = await latest_option_snapshot(db, call.id)
            assert snapshot is not None
            assert float(snapshot.bid) == 101.0 and float(snapshot.ask) == 102.0
            assert float(snapshot.volume) == 12345
            assert float(snapshot.delta) == 0.55
    finally:
        await _cleanup_instruments(underlying, call, put)


async def test_every_quote_in_one_fetch_shares_one_timestamp(require_infra):
    """Behavioural proof. `POST /options/execute` reports the *worst*
    staleness across a strategy's legs, so a per-row clock would make legs
    written later in the loop look fresher than legs written first --
    a difference invented by the writer, not by the market."""
    prefix = f"TSM{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    provider = _StubChainProvider(_chain_payload(call.broker_instrument_key, put.broker_instrument_key))
    try:
        async with async_session_factory() as db:
            await ingest_option_chain(db, provider, underlying, call.expiry)
            await db.commit()

        async with async_session_factory() as db:
            call_snapshot = await latest_option_snapshot(db, call.id)
            put_snapshot = await latest_option_snapshot(db, put.id)
            assert call_snapshot.snapshot_at == put_snapshot.snapshot_at
    finally:
        await _cleanup_instruments(underlying, call, put)


async def test_a_contract_this_deployment_has_not_registered_is_reported_not_invented(require_infra):
    """Behavioural proof. Registering an instrument is `POST /instruments`'
    job; creating rows here would let a provider's spelling of a symbol
    quietly become this system's."""
    prefix = f"UNR{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    # Unique per run, not a fixed literal: a stray row carrying the same
    # key -- from another test, or from a deliberately broken build during
    # an injection run -- would otherwise make this contract "registered"
    # and quietly invert what the test measures.
    unknown_key = f"NSE_FO|NOT-REGISTERED-{uuid.uuid4().hex[:8].upper()}"
    provider = _StubChainProvider(_chain_payload(call.broker_instrument_key, unknown_key))
    try:
        async with async_session_factory() as db:
            result = await ingest_option_chain(db, provider, underlying, call.expiry)
            await db.commit()

        assert result.snapshots_written == 1
        assert result.unregistered_count == 1
        assert result.unregistered_sample == [unknown_key]

        async with async_session_factory() as db:
            assert await latest_option_snapshot(db, put.id) is None
            created = (
                await db.execute(
                    select(Instrument).where(Instrument.broker_instrument_key == unknown_key)
                )
            ).scalar_one_or_none()
            assert created is None, "ingestion must not mint instrument rows"
    finally:
        await _cleanup_instruments(underlying, call, put)


async def test_a_chain_with_no_spot_price_is_refused_rather_than_stored_at_zero(require_infra):
    """Behavioural proof. `OptionChainSnapshot.spot_price` is NOT NULL, and
    0.0 would be a fabricated price in the store -- the exact failure this
    codebase has removed from two risk paths."""
    prefix = f"NSP{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    payload = _chain_payload(call.broker_instrument_key, put.broker_instrument_key)
    del payload["data"][0]["underlying_spot_price"]
    provider = _StubChainProvider(payload)
    try:
        async with async_session_factory() as db:
            with pytest.raises(ChainSpotPriceUnavailable) as exc:
                await ingest_option_chain(db, provider, underlying, call.expiry)
            await db.rollback()
        assert underlying.symbol in str(exc.value)

        async with async_session_factory() as db:
            assert await latest_option_snapshot(db, call.id) is None
    finally:
        await _cleanup_instruments(underlying, call, put)


# --- the reader had to survive the writer --------------------------------


async def test_a_second_fetch_does_not_break_the_reader(require_infra):
    """Behavioural proof, and a regression test for a measured 500.

    Both readers resolved the contract with `.scalar_one_or_none()`.
    Running ingestion twice creates a second `OptionContract` for the same
    instrument, and the next `POST /options/execute` raised
    `MultipleResultsFound` -- uncaught, so a 500. The fix reads the newest
    snapshot across every contract, which is the question both callers
    were actually asking.
    """
    prefix = f"TWO{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    first = _chain_payload(call.broker_instrument_key, put.broker_instrument_key)
    second = _chain_payload(call.broker_instrument_key, put.broker_instrument_key)
    second["data"][0]["call_options"]["market_data"]["bid_price"] = 111.0
    second["data"][0]["call_options"]["market_data"]["ask_price"] = 112.0
    try:
        async with async_session_factory() as db:
            await ingest_option_chain(db, _StubChainProvider(first), underlying, call.expiry)
            await db.commit()
        async with async_session_factory() as db:
            await ingest_option_chain(db, _StubChainProvider(second), underlying, call.expiry)
            await db.commit()

        async with async_session_factory() as db:
            contracts = (
                await db.execute(select(OptionContract).where(OptionContract.instrument_id == call.id))
            ).scalars().all()
            assert len(contracts) == 2, "the trap only exists once there is more than one"

            snapshot = await latest_option_snapshot(db, call.id)
            assert snapshot is not None
            # The newest quote, not whichever contract the database
            # happened to return first.
            assert float(snapshot.bid) == 111.0
    finally:
        await _cleanup_instruments(underlying, call, put)


async def test_an_instrument_with_no_chain_at_all_still_reads_as_none(require_infra):
    """Control. The reader must keep answering "nothing here" rather than
    raising or inventing a quote -- that `None` is what makes the execution
    gates record "not assessed" instead of a fabricated pass."""
    prefix = f"NIL{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    try:
        async with async_session_factory() as db:
            assert await latest_option_snapshot(db, call.id) is None
    finally:
        await _cleanup_instruments(underlying, call, put)


async def test_the_reader_ignores_other_instruments_snapshots(require_infra):
    """Control, and the one that stops the join being too generous. A quote
    for the put must never answer a question about the call -- that would
    gate one leg on another leg's liquidity."""
    prefix = f"SEP{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    payload = _chain_payload(f"NSE_FO|NOT-REGISTERED-{uuid.uuid4().hex[:8].upper()}", put.broker_instrument_key)
    try:
        async with async_session_factory() as db:
            await ingest_option_chain(db, _StubChainProvider(payload), underlying, call.expiry)
            await db.commit()

        async with async_session_factory() as db:
            assert await latest_option_snapshot(db, put.id) is not None
            assert await latest_option_snapshot(db, call.id) is None
    finally:
        await _cleanup_instruments(underlying, call, put)


async def test_the_newest_snapshot_wins_even_when_written_out_of_order(require_infra):
    """Control for the ordering. Rows inserted newest-first must still
    resolve to the newest quote, so the answer comes from `snapshot_at`
    rather than from insertion order."""
    prefix = f"ORD{uuid.uuid4().hex[:5].upper()}"
    underlying, call, put = await _make_underlying_and_contracts(prefix)
    try:
        now = datetime.now(timezone.utc)
        async with async_session_factory() as db:
            for offset, bid in ((0, 50.0), (-3600, 10.0)):
                chain = OptionChainSnapshot(
                    underlying=underlying.symbol, expiry=call.expiry, spot_price=1.0, fetched_at=now
                )
                db.add(chain)
                await db.flush()
                contract = OptionContract(
                    instrument_id=call.id, chain_id=chain.id, strike=25000.0, option_type="CE"
                )
                db.add(contract)
                await db.flush()
                db.add(
                    OptionSnapshot(
                        option_contract_id=contract.id,
                        bid=bid,
                        ask=bid + 1,
                        ltp=bid,
                        volume=1,
                        open_interest=1,
                        snapshot_at=now + timedelta(seconds=offset),
                    )
                )
            await db.commit()

        async with async_session_factory() as db:
            snapshot = await latest_option_snapshot(db, call.id)
            assert float(snapshot.bid) == 50.0
    finally:
        await _cleanup_instruments(underlying, call, put)
