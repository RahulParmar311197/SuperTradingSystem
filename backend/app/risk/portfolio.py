"""Portfolio-level risk (blueprint §86): real exposure and market-type
breakdown across a user's open positions, plus the DB-orchestration layer
for the correlation engine (`app.risk.correlation`) — looking up
instruments and candle history so a correlated-exposure number can be
computed for the risk engine's `correlated_exposure_limit` check.

`app.risk.correlation` stays pure (no DB/async) so its math is unit
tested directly; this module is the integration point that feeds it real
data.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.trading.persistence import ACCOUNT_BACKED_SOURCE_KEYS, PAPER_SANDBOX_TRADE_SOURCE

from app.database.models.instruments import Instrument
from app.database.models.trading import ExecutionMode, Position, Trade
from app.market.repository import get_candles
from app.risk.correlation import build_correlation_matrix, closes_by_timestamp
from app.risk.correlation import correlated_exposure as _correlated_exposure


@dataclass(slots=True)
class PortfolioExposure:
    total_exposure: float
    exposure_by_market: dict[str, float] = field(default_factory=dict)
    # Summed over the `trades` journal for this account, not over
    # `positions` -- see the note in `compute_portfolio_exposure` for why
    # the position rows cannot be summed.
    total_realized_pnl: float = 0.0


async def compute_portfolio_exposure(
    db: AsyncSession, user_id: uuid.UUID, execution_mode: ExecutionMode = ExecutionMode.LIVE
) -> PortfolioExposure:
    """Total notional and per-market-type breakdown across a user's open
    positions, read from the real `positions` table, plus realized P&L
    across every trade the account has closed in this execution mode.

    Exposure is an open-positions figure -- a closed position has no
    notional at risk. Realized P&L is the opposite: it only exists *because*
    a position closed, so restricting it to open positions reports 0.0 for a
    fully closed trade, which is what `GET /portfolio` used to do when it
    derived the number from the in-memory manager's `open_positions()`.

    It comes from `trades`, not from `positions`, because
    `Position.realized_pnl` is not an increment. `PositionManager.apply_fill`
    keeps one `PositionRecord` per (account, symbol) forever and does
    `realized_pnl += realized` on it, so the value is a running lifetime
    total; `app.trading.persistence.persist_position` looks up only the
    *open* row, so re-entering an instrument inserts a new row seeded with
    that lifetime total, and summing the rows counts every earlier round
    trip again -- two closed round trips of +500 and +300 summed to 1300
    instead of 800. Each `Trade` row, by contrast, carries the realized
    delta of exactly one fill (`pnl=realized_delta` in app/api/orders.py and
    app/api/options.py, `pnl=outcome.closed_position_pnl` in app/api/paper.py
    and app/workers/auto_trade_worker.py), so the journal sums correctly for
    partial closes and flips as well. It also survives an API restart, which
    resets the in-memory counter to 0 and starts a fresh row chain.
    """
    positions = (
        await db.execute(
            select(Position).where(
                Position.user_id == user_id,
                Position.execution_mode == execution_mode,
                # The account's OWN partitions only. A `/paper/{id}` session
                # is a sandbox with its own `starting_balance` that consumes
                # none of the account's capital -- PR #179 excluded it from
                # the exposure GATE for exactly that reason, and the same
                # reasoning says it does not belong in the account's
                # reported figure either. Measured through the endpoints:
                # one real 50,000 position plus one live sandbox position
                # reported `total_exposure` 65,606.06.
                #
                # This is an ALLOWLIST, deliberately, while the trades
                # filter below is a denylist -- see the note there. The
                # partition set is closed and named in one place, and PR
                # #179's gate already keys off the same tuple, so the two
                # cannot drift apart without its partition-set test saying
                # so.
                Position.source_key.in_(ACCOUNT_BACKED_SOURCE_KEYS),
                Position.is_open.is_(True),
            )
        )
    ).scalars().all()

    total_realized_pnl = float(
        (
            await db.execute(
                select(func.coalesce(func.sum(Trade.pnl), 0)).where(
                    Trade.user_id == user_id,
                    Trade.execution_mode == execution_mode,
                    # The same exclusion on the realized side. A sandbox
                    # that runs to target journals a `trades` row like any
                    # other close, so simulated profit was being reported
                    # as the account's: measured 5,000.00 -> 6,000.00 from
                    # one sandbox trade.
                    #
                    # A DENYLIST, unlike the positions filter above, and
                    # the asymmetry is deliberate. `Trade` has no
                    # `source_key`; `position_id` is NULL on both the paper
                    # and auto writers, so it cannot separate them either.
                    # The discriminator is `journal.source`, and that field
                    # has a legacy NULL state -- rows written before
                    # `AUTO_TRADE_SOURCE` existed, which
                    # `_written_by_the_auto_trade_worker` already has to
                    # special-case. An allowlist would silently drop those,
                    # under-reporting realized P&L; naming only the sandbox
                    # keeps every unknown row counted.
                    Trade.journal["source"].as_string().is_distinct_from(PAPER_SANDBOX_TRADE_SOURCE),
                )
            )
        ).scalar_one()
    )

    total = 0.0
    by_market: dict[str, float] = {}
    for position in positions:
        instrument = await db.get(Instrument, position.instrument_id)
        if instrument is None:
            continue
        notional = abs(float(position.quantity) * float(position.average_price))
        total += notional
        by_market[instrument.market.value] = by_market.get(instrument.market.value, 0.0) + notional

    return PortfolioExposure(
        total_exposure=total, exposure_by_market=by_market, total_realized_pnl=total_realized_pnl
    )


def signed_notionals_excluding(positions, exclude_symbol: str) -> dict[str, float]:
    """`{symbol: signed notional}` for every open position but one.

    Signed -- negative for a short -- because `correlated_exposure` nets
    rather than sums, so an `abs()` here turns a hedge into double
    concentration and blocks the trade that reduced the risk.

    This exists as a function rather than as a dict comprehension inlined
    at each call site because it was inlined at each call site, in
    app/api/orders.py and app/paper/engine.py, whose comments already said
    the two must mirror each other. Nothing could test that claim, and
    nothing did: re-adding `abs()` to both left the entire suite green.
    One named contract with one test is what makes the invariant
    enforceable rather than merely stated.

    `positions` is anything with `.symbol`, `.quantity` and
    `.average_price` -- `PositionRecord` in both callers. Typed loosely so
    this module does not have to import the execution layer to name it.
    """
    return {
        p.symbol: p.quantity * p.average_price
        for p in positions
        if p.symbol != exclude_symbol
    }


async def compute_correlated_exposure(
    db: AsyncSession,
    target_symbol: str,
    target_notional: float,
    open_position_notionals: dict[str, float],
    threshold: float,
    timeframe: str = "15m",
    lookback: int = 100,
) -> float:
    """Correlated exposure for a proposed `target_symbol` position, using
    real close-to-close returns from whatever candle history each
    instrument already has (`app.market.repository.get_candles`). A
    symbol with no registered instrument or too little candle history
    simply contributes no correlation data — never a hard failure, since
    correlation is a refinement on top of the exposure check, not a
    replacement for it.

    `open_position_notionals` and the returned value are **signed**:
    negative for a short, and negative overall when the correlated book
    leans opposite to the target. Callers must pass the sign through
    rather than `abs()`-ing it — see
    `app.risk.correlation.correlated_exposure`, which nets rather than
    sums so that a hedge is not counted as concentration."""
    symbols = {target_symbol, *open_position_notionals}
    closes_by_symbol: dict[str, dict[datetime, float]] = {}
    for symbol in symbols:
        instrument = (
            await db.execute(select(Instrument).where(Instrument.symbol == symbol))
        ).scalar_one_or_none()
        if instrument is None:
            continue
        candles = await get_candles(db, instrument.id, timeframe)
        if len(candles) >= 3:
            # Keyed by timestamp, not flattened to a bare return list:
            # `build_correlation_matrix` intersects each pair on the bars
            # they genuinely share before computing returns, so two
            # instruments with different candle coverage are never
            # correlated position-by-position across mismatched periods.
            closes_by_symbol[symbol] = closes_by_timestamp(candles[-lookback:])

    matrix = build_correlation_matrix(closes_by_symbol)
    return _correlated_exposure(target_symbol, target_notional, open_position_notionals, matrix, threshold)
