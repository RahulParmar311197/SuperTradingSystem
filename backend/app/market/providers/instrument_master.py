"""Resolving this system's symbols to a provider's instrument identifiers.

`Instrument.symbol` is a trading symbol ("INFY", "NIFTY"). Upstox names
instruments as `"NSE_EQ|INE009A01021"` or `"NSE_INDEX|Nifty 50"` and will
not accept anything else, so every market-data call needs a translation
that nothing in this codebase previously had.

The mapping comes from Upstox's published instrument master (a JSON array,
one object per tradable instrument). Parsing and matching live here as
pure functions over already-loaded records, so both are testable without
network access and without a token -- fetching the file is the caller's
problem, and a deliberately small one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.instruments import Instrument

logger = logging.getLogger("market.providers.instrument_master")


class InstrumentKeyUnknown(LookupError):
    """Raised at the point of use when an instrument has no provider key."""


def resolve_instrument_key(instrument: Instrument) -> str:
    """The provider identifier for `instrument`, or a legible failure.

    Deliberately raises rather than returning the plain symbol as a
    fallback. A symbol Upstox does not recognise comes back as an opaque
    rejection from the far end, several layers from the cause; naming the
    instrument and the remedy here is the same ruling the broker resolver
    makes for an account it cannot build an adapter for.
    """
    key = (instrument.broker_instrument_key or "").strip()
    if not key:
        raise InstrumentKeyUnknown(
            f"Instrument {instrument.symbol!r} has no broker_instrument_key, so no market-data "
            "provider can be asked about it. Load a provider instrument master "
            "(app.market.providers.instrument_master.apply_instrument_keys) first."
        )
    return key


def parse_master_records(records: list[dict]) -> dict[tuple[str, str], str]:
    """Upstox master records indexed by `(EXCHANGE, TRADING_SYMBOL)`.

    Upper-cased on both sides because the file is not consistent with
    itself: equities carry an upper-case `trading_symbol` ("INFY") while
    indices carry a title-cased one ("Nifty 50"). Matching exactly would
    silently miss every index -- which, on an NSE-focused platform, is
    most of what anyone wants to trade.

    A record without both an `instrument_key` and a `trading_symbol` is
    skipped and counted in the log rather than stored half-formed.
    """
    index: dict[tuple[str, str], str] = {}
    skipped = 0
    for record in records:
        if not isinstance(record, dict):
            skipped += 1
            continue
        key = (record.get("instrument_key") or "").strip()
        symbol = (record.get("trading_symbol") or "").strip()
        exchange = (record.get("exchange") or "").strip()
        if not key or not symbol or not exchange:
            skipped += 1
            continue
        index[(exchange.upper(), symbol.upper())] = key
    if skipped:
        logger.info("Skipped %d instrument master records with no key/symbol/exchange", skipped)
    return index


@dataclass(slots=True)
class InstrumentKeyReport:
    """What a master load actually did, including what it could not do.

    `unmatched` is the point of this type. An instrument the master file
    has no entry for stays unusable for market data, and that has to be
    visible to whoever ran the load -- a silent partial success here means
    discovering the gap later as a backfill that returns nothing.
    """

    matched: dict[str, str] = field(default_factory=dict)
    unchanged: list[str] = field(default_factory=list)
    unmatched: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.unmatched


async def apply_instrument_keys(
    db: AsyncSession, index: dict[tuple[str, str], str], *, overwrite: bool = False
) -> InstrumentKeyReport:
    """Populate `broker_instrument_key` for every instrument the index
    covers, and report the ones it does not.

    `overwrite=False` by default: a key already stored was either loaded
    from a previous master or set by hand, and re-running a load should
    not quietly replace a hand-corrected value. Pass `overwrite=True` when
    the intent is genuinely to re-seed from the file.
    """
    report = InstrumentKeyReport()
    instruments = (await db.execute(select(Instrument))).scalars().all()
    for instrument in instruments:
        existing = (instrument.broker_instrument_key or "").strip()
        if existing and not overwrite:
            report.unchanged.append(instrument.symbol)
            continue
        key = index.get((instrument.exchange.upper(), instrument.symbol.upper()))
        if key is None:
            report.unmatched.append(instrument.symbol)
            continue
        instrument.broker_instrument_key = key
        report.matched[instrument.symbol] = key
    await db.commit()
    if report.unmatched:
        logger.warning(
            "No instrument master entry for %d symbol(s): %s", len(report.unmatched), ", ".join(report.unmatched)
        )
    return report
