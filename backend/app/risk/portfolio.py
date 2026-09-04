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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.instruments import Instrument
from app.database.models.trading import ExecutionMode, Position
from app.market.repository import get_candles
from app.risk.correlation import build_correlation_matrix, close_returns
from app.risk.correlation import correlated_exposure as _correlated_exposure


@dataclass(slots=True)
class PortfolioExposure:
    total_exposure: float
    exposure_by_market: dict[str, float] = field(default_factory=dict)


async def compute_portfolio_exposure(
    db: AsyncSession, user_id: uuid.UUID, execution_mode: ExecutionMode = ExecutionMode.LIVE
) -> PortfolioExposure:
    """Total notional and per-market-type breakdown across a user's open
    positions, read from the real `positions` table."""
    positions = (
        await db.execute(
            select(Position).where(
                Position.user_id == user_id,
                Position.execution_mode == execution_mode,
                Position.is_open.is_(True),
            )
        )
    ).scalars().all()

    total = 0.0
    by_market: dict[str, float] = {}
    for position in positions:
        instrument = await db.get(Instrument, position.instrument_id)
        if instrument is None:
            continue
        notional = abs(float(position.quantity) * float(position.average_price))
        total += notional
        by_market[instrument.market.value] = by_market.get(instrument.market.value, 0.0) + notional

    return PortfolioExposure(total_exposure=total, exposure_by_market=by_market)


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
    returns_by_symbol: dict[str, list[float]] = {}
    for symbol in symbols:
        instrument = (
            await db.execute(select(Instrument).where(Instrument.symbol == symbol))
        ).scalar_one_or_none()
        if instrument is None:
            continue
        candles = await get_candles(db, instrument.id, timeframe)
        if len(candles) >= 3:
            returns_by_symbol[symbol] = close_returns(candles[-lookback:])

    matrix = build_correlation_matrix(returns_by_symbol)
    return _correlated_exposure(target_symbol, target_notional, open_position_notionals, matrix, threshold)
