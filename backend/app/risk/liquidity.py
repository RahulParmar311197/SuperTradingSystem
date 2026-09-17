"""Equity liquidity as a participation rate (blueprint §57, §85).

An earlier round found that `TradeRiskProposal.liquidity_acceptable`
defaulted to `True` and had no writer, so every equity order's `RiskEvent`
recorded a passed liquidity check that nothing had performed. That was
fixed by making the field `bool | None` and skipping it when unset -- an
honest audit row, but still no gate. **This is the gate**, and it is the
first time equities have had one.

The question it answers is not "is this instrument liquid" in the
abstract. It is: *can this order be filled without the order itself moving
the price against us.* That is a property of the order and the instrument
together, so the measure is a **participation rate** -- the order's
quantity as a share of what typically trades in a bar -- and not an
absolute floor on volume. An absolute floor gets both ends wrong: it
blocks a tiny order in a thin name that would fill fine, and waves through
an enormous one in a liquid name that would not.

The options side (`app/options/liquidity_filter.py`) deliberately does
not share this. Its thresholds are open interest and bid/ask spread on a
specific contract, which are facts about the contract; there is no
equivalent for an NSE equity here, and reusing option-shaped numbers for
one would be inventing a limit rather than choosing it.

**The default is reasoned, not calibrated.** There is no licensed feed in
this environment, so `max_participation_pct` is set from standard
practice rather than measured against real NSE volume -- see
`RiskLimits.max_participation_pct`. It is the first number to revisit
once real data exists.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.instruments import Instrument
from app.market.repository import get_candles

logger = logging.getLogger("risk.liquidity")

# Bars of history averaged over. At the 15m default this is five hours --
# most of an NSE session -- which is long enough that one unusually quiet
# or busy bar cannot decide the verdict, and short enough to still
# describe today's market rather than last month's.
DEFAULT_VOLUME_WINDOW_BARS = 20


def assess_participation(
    quantity: float,
    average_bar_volume: float | None,
    max_participation_pct: float,
) -> bool | None:
    """`True` / `False` / `None` for "could not assess".

    `None` propagates the distinction the risk engine already draws twice
    (`market_data_age_seconds`, and this field itself): *not assessed* is
    not the same claim as *assessed and fine*, and recording the second
    when only the first happened is the fabricated audit row this whole
    line of work started from. A caller with no candle history for the
    symbol gets `None` and the check is skipped, not passed.

    An average of exactly 0.0 is a different fact and **rejects**. That is
    not missing data: it is data saying nothing traded across the whole
    window. An order into it cannot fill at any price we could reason
    about, and a participation rate against zero is undefined rather than
    small.
    """
    if average_bar_volume is None:
        return None
    if average_bar_volume <= 0:
        return False
    return (quantity / average_bar_volume) * 100 <= max_participation_pct


async def average_bar_volume(
    db: AsyncSession,
    symbol: str,
    timeframe: str = "15m",
    window: int = DEFAULT_VOLUME_WINDOW_BARS,
) -> float | None:
    """Mean `Candle.volume` over the last `window` bars, or `None`.

    `None` when the symbol has no registered instrument or no candles at
    all -- the caller cannot assess what it cannot see, and says so rather
    than guessing. Averaged over however many bars exist when there are
    fewer than `window`, since a short history is still evidence; only no
    history at all is silence.
    """
    instrument = (
        await db.execute(select(Instrument).where(Instrument.symbol == symbol))
    ).scalar_one_or_none()
    if instrument is None:
        return None
    candles = await get_candles(db, instrument.id, timeframe)
    if not candles:
        return None
    recent = candles[-window:]
    return sum(c.volume for c in recent) / len(recent)


async def assess_equity_liquidity(
    db: AsyncSession | None,
    symbol: str,
    quantity: float,
    max_participation_pct: float,
    timeframe: str = "15m",
    window: int = DEFAULT_VOLUME_WINDOW_BARS,
) -> bool | None:
    """What both order paths call: the verdict for one proposed order.

    One function rather than the same few lines at each call site, and
    deliberately so. The immediately preceding round found a dict
    comprehension written out twice in these same two files, whose own
    comments said the two had to mirror each other -- and re-breaking both
    at once left the entire test suite green, because nothing exercised
    either copy. A shared contract with a test is the difference between
    an invariant that is stated and one that is enforceable.

    `db` is optional because `PaperTradingEngine` can run without a
    session (unit tests that exercise the engine with no database). No
    session means no assessment means `None`, never a fabricated pass.
    """
    if db is None:
        return None
    try:
        average = await average_bar_volume(db, symbol, timeframe, window)
    except Exception:
        # A liquidity assessment is a refinement on the exposure checks,
        # not a precondition for them, so a database hiccup here must not
        # take down the order path. It downgrades to "not assessed", which
        # skips the check rather than passing it.
        logger.exception("Could not read volume history for %s; liquidity not assessed", symbol)
        return None
    return assess_participation(quantity, average, max_participation_pct)
