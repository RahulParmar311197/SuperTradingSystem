"""`PortfolioSnapshot` journaling (blueprint §9 `portfolio_snapshots`) for
every user with a live trading stack in this process.

Callable on demand (`POST /admin/portfolio-snapshot`) rather than an
automatic background loop: this can only run inside the API process, not
the separate `worker` process, for the same reason
`app.trading.live_reconciliation` runs there — it needs the same
broker/`PositionManager` instances a user's orders were placed through
(`app.api.orders._STACKS`), which only exist in this process's memory. A
background loop was tried and dropped: unlike reconciliation (bounded by
the small, DB-backed set of ACTIVE `BrokerAccount` rows),
`app.api.orders._STACKS` only ever grows for the life of the process, so
an immediate-on-startup pass over it does not stay cheap the way
reconciliation's does. A real deployment should trigger this from an
external scheduler instead.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.orders import _execution_mode_for, _UserTradingStack, all_stacks
from app.database.models.instruments import Instrument
from app.database.models.options import OptionContract, OptionSnapshot
from app.database.models.trading import PortfolioSnapshot
from app.database.session import async_session_factory
from app.risk.portfolio import compute_portfolio_exposure

logger = logging.getLogger("trading.portfolio_snapshots")


async def _net_greeks(db: AsyncSession, positions) -> tuple[float, float, float, float]:
    """Best-effort net delta/gamma/theta/vega across a user's open
    positions. Only contributes for a position whose instrument is an
    option (`Instrument.option_type` is set) AND already has a real
    `OptionSnapshot` -- there is no options-chain ingestion pipeline in
    this environment (see docs/ARCHITECTURE.md), so most positions will
    simply contribute 0 rather than a fabricated Greek."""
    net_delta = net_gamma = net_theta = net_vega = 0.0
    for position in positions:
        instrument = (
            await db.execute(select(Instrument).where(Instrument.symbol == position.symbol))
        ).scalar_one_or_none()
        if instrument is None or instrument.option_type is None:
            continue

        contract = (
            await db.execute(select(OptionContract).where(OptionContract.instrument_id == instrument.id))
        ).scalar_one_or_none()
        if contract is None:
            continue

        snapshot = (
            await db.execute(
                select(OptionSnapshot)
                .where(OptionSnapshot.option_contract_id == contract.id)
                .order_by(OptionSnapshot.snapshot_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if snapshot is None:
            continue

        net_delta += position.quantity * float(snapshot.delta or 0.0)
        net_gamma += position.quantity * float(snapshot.gamma or 0.0)
        net_theta += position.quantity * float(snapshot.theta or 0.0)
        net_vega += position.quantity * float(snapshot.vega or 0.0)

    return net_delta, net_gamma, net_theta, net_vega


async def _snapshot_one(db: AsyncSession, user_id: uuid.UUID, stack: _UserTradingStack) -> None:
    execution_mode = _execution_mode_for(stack)
    account = await stack.broker.get_account()
    positions = stack.position_manager.open_positions(str(user_id))
    exposure = await compute_portfolio_exposure(db, user_id, execution_mode=execution_mode)
    net_delta, net_gamma, net_theta, net_vega = await _net_greeks(db, positions)

    db.add(
        PortfolioSnapshot(
            user_id=user_id,
            execution_mode=execution_mode,
            balance=account.balance,
            equity=account.equity,
            total_exposure=exposure.total_exposure,
            net_delta=net_delta,
            net_gamma=net_gamma,
            net_theta=net_theta,
            net_vega=net_vega,
            snapshot_at=datetime.now(timezone.utc),
        )
    )


async def snapshot_all_stacks() -> int:
    """Writes one `PortfolioSnapshot` row for every trading stack that
    currently has at least one open position. Returns how many were
    written.

    `app.api.orders._STACKS` never shrinks -- every user who has ever
    placed an order during this process's lifetime stays in it, whether
    or not they still hold anything. Skipping a stack with nothing open
    right now isn't just an optimization: without it, this loop's cost
    grows without bound over a long-running process's lifetime, snapshotting
    accounts with nothing left to report.

    Each user gets its own session/commit rather than one shared
    transaction for the whole pass, so a problem with one account's data
    can't roll back every other account's otherwise-valid snapshot.
    """
    written = 0
    for user_id, stack in all_stacks().items():
        if not stack.position_manager.open_positions(str(user_id)):
            continue
        async with async_session_factory() as db:
            try:
                await _snapshot_one(db, user_id, stack)
                await db.commit()
                written += 1
            except Exception:
                await db.rollback()
                logger.exception("Portfolio snapshot failed for user %s", user_id)
    return written
