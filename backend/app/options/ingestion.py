"""Writing an option chain into the store (blueprint §40, §52).

**`option_snapshots` had three readers and no writer.** `POST
/options/execute` reads it for all three of its options-specific gates --
liquidity, premium deviation against the real mid, and quote staleness --
and `app/trading/portfolio_snapshots.py` reads it to mark option positions
to market. Nothing in `app/` ever inserted a row, so every one of those was
inert: an earlier round had to make the endpoint record `None` for each
check rather than let the audit row claim a check that never ran.

This is the missing half. It is the options analogue of
`app.market.backfill.backfill_candles` and is driven the same way, from
`POST /admin/option-chain` rather than a background loop: which expiry to
fetch is a judgement call and provider chains are rate-limited.

Append-only on purpose. Each run writes a new `option_chains` row with
fresh `option_contracts` and `option_snapshots` beneath it, because a
snapshot *is* a timestamped quote and yesterday's is not wrong, merely
old. That is also why `app.options.snapshots.latest_option_snapshot` had
to stop assuming one contract per instrument: the second run of this
function is what would otherwise have made it raise.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.instruments import Instrument
from app.database.models.options import OptionChainSnapshot, OptionContract, OptionSnapshot
from app.market.providers.instrument_master import resolve_instrument_key

logger = logging.getLogger("options.ingestion")

# How many unmatched provider keys to name in the result. The whole list
# would be most of a Nifty chain on a deployment that has registered two
# strikes, which is noise rather than a diagnosis; a handful is enough to
# paste into `POST /instruments`.
_UNREGISTERED_SAMPLE = 10


@dataclass(slots=True)
class OptionChainIngestResult:
    underlying: str
    expiry: date
    fetched_at: datetime
    spot_price: float
    quotes_returned: int
    snapshots_written: int
    # Contracts the provider quoted that this deployment has no
    # `Instrument` row for. Reported rather than auto-created: registering
    # an instrument is `POST /instruments`' job, and inventing rows here
    # would let a provider's spelling of a symbol become this system's.
    unregistered_count: int = 0
    unregistered_sample: list[str] = field(default_factory=list)


class ChainSpotPriceUnavailable(ValueError):
    """The chain came back without the underlying's spot price.

    `OptionChainSnapshot.spot_price` is NOT NULL, and writing 0.0 would
    put a fabricated price in the store — the specific failure this
    codebase has now removed from two risk paths. Refusing is the honest
    outcome, and it names what to do about it.
    """


async def ingest_option_chain(
    db: AsyncSession,
    market_data,
    underlying: Instrument,
    expiry: date,
) -> OptionChainIngestResult:
    """Fetch `underlying`'s chain for `expiry` and store every quote whose
    contract this deployment has registered.

    `market_data` is anything with `get_option_chain` — the read-only
    `UpstoxMarketData` in production, a stub in tests. Typed loosely for
    the same reason `backfill_candles` is: this module must not import a
    client that holds a credential merely to name its type.
    """
    key = resolve_instrument_key(underlying)
    chain = await market_data.get_option_chain(key, expiry)

    if chain.underlying_spot_price is None:
        raise ChainSpotPriceUnavailable(
            f"The option chain for {underlying.symbol!r} ({key}) at expiry {expiry} carried no "
            "underlying_spot_price, so the chain cannot be recorded without inventing one. "
            "Retry during market hours, or check that the instrument key names the underlying "
            "rather than a contract."
        )

    # One lookup for the whole chain rather than one per quote: a Nifty
    # expiry is several hundred sides, and this runs inside a request.
    quoted_keys = {q.instrument_key for q in chain.quotes}
    registered: dict[str, uuid.UUID] = {
        row.broker_instrument_key: row.id
        for row in (
            await db.execute(select(Instrument).where(Instrument.broker_instrument_key.in_(quoted_keys)))
        ).scalars()
        if row.broker_instrument_key
    }

    fetched_at = datetime.now(timezone.utc)
    chain_row = OptionChainSnapshot(
        underlying=underlying.symbol,
        expiry=expiry,
        spot_price=chain.underlying_spot_price,
        fetched_at=fetched_at,
    )
    db.add(chain_row)
    await db.flush()

    written = 0
    unregistered: list[str] = []
    for quote in chain.quotes:
        instrument_id = registered.get(quote.instrument_key)
        if instrument_id is None:
            unregistered.append(quote.instrument_key)
            continue
        contract = OptionContract(
            instrument_id=instrument_id,
            chain_id=chain_row.id,
            strike=quote.strike,
            option_type=quote.option_type,
        )
        db.add(contract)
        await db.flush()
        db.add(
            OptionSnapshot(
                option_contract_id=contract.id,
                bid=quote.bid,
                ask=quote.ask,
                ltp=quote.ltp,
                volume=quote.volume,
                open_interest=quote.open_interest,
                iv=quote.iv,
                delta=quote.delta,
                gamma=quote.gamma,
                theta=quote.theta,
                vega=quote.vega,
                # The fetch time, shared by every quote in this chain --
                # not `now()` per row. `POST /options/execute` reports the
                # worst staleness across a strategy's legs, and a
                # per-row clock would make legs written later in the same
                # loop look fresher than legs written first.
                snapshot_at=fetched_at,
            )
        )
        written += 1

    logger.info(
        "Ingested %d of %d quoted sides for %s expiry %s (spot %s); %d contracts are not registered here",
        written,
        len(chain.quotes),
        underlying.symbol,
        expiry,
        chain.underlying_spot_price,
        len(unregistered),
    )
    return OptionChainIngestResult(
        underlying=underlying.symbol,
        expiry=expiry,
        fetched_at=fetched_at,
        spot_price=chain.underlying_spot_price,
        quotes_returned=len(chain.quotes),
        snapshots_written=written,
        unregistered_count=len(unregistered),
        unregistered_sample=sorted(unregistered)[:_UNREGISTERED_SAMPLE],
    )
