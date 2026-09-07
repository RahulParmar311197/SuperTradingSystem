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
    replacement for it."""
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
