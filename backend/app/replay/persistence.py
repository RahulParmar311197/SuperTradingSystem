"""Mirrors `ReplayEngine` state into Postgres (blueprint §9:
`replay_sessions`, `replay_orders`) after every mutating action — the
same pattern `app.trading.persistence` uses for manual live orders. The
in-memory engine (`app/api/replay.py`'s `_SESSIONS`) stays the source of
truth for the live process; this is what survives a restart, and what
lets `GET /replay/{id}` verify a session actually belongs to the caller
instead of trusting any authenticated user with any UUID.
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.replay import ReplayOrder, ReplaySession, ReplayStatus
from app.replay.engine import ReplayEngine, ReplayTrade


async def create_replay_session_row(
    db: AsyncSession,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    timeframe: str,
    engine: ReplayEngine,
) -> ReplaySession:
    row = ReplaySession(
        id=session_id,
        user_id=user_id,
        instrument_id=instrument_id,
        timeframe=timeframe,
        start_time=engine.clock.candles[0].timestamp,
        end_time=engine.clock.candles[-1].timestamp,
        current_time=engine.clock.current_candle.timestamp,
        speed=engine.clock.speed,
        status=ReplayStatus(engine.clock.status.value),
        starting_balance=engine.starting_balance,
        balance=engine.balance,
        stats={},
    )
    db.add(row)
    await db.commit()
    return row


async def get_owned_replay_session(db: AsyncSession, session_id: uuid.UUID, user_id: uuid.UUID) -> ReplaySession | None:
    row = await db.get(ReplaySession, session_id)
    if row is None or row.user_id != user_id:
        return None
    return row


async def _upsert_replay_order(db: AsyncSession, session_id: uuid.UUID, trade: ReplayTrade) -> None:
    row = await db.get(ReplayOrder, trade.id)
    if row is None:
        row = ReplayOrder(
            id=trade.id,
            replay_session_id=session_id,
            direction=trade.direction.value,
            entry_price=trade.entry_price,
            quantity=trade.quantity,
            opened_at=trade.opened_at,
        )
        db.add(row)
    row.stop = trade.stop
    row.target = trade.target
    row.exit_price = trade.exit_price
    row.pnl = trade.pnl
    row.closed_at = trade.closed_at


async def sync_replay_session(db: AsyncSession, session_id: uuid.UUID, engine: ReplayEngine) -> None:
    """Call after any action that mutates `engine` (step/reset/order) so
    the persisted row never drifts from the live in-memory state."""
    row = await db.get(ReplaySession, session_id)
    if row is None:
        return

    row.current_time = engine.clock.current_candle.timestamp
    row.status = ReplayStatus(engine.clock.status.value)
    row.balance = engine.balance
    # ReplayStatistics is `@dataclass(slots=True)`, which has no `__dict__`
    # — `.__dict__` raises AttributeError on it (and on every other
    # slots=True dataclass in this codebase; see app/api/replay.py,
    # app/api/ai.py, app/api/backtest.py, app/api/markets.py).
    row.stats = dataclasses.asdict(engine.statistics)

    if engine.open_trade is not None:
        await _upsert_replay_order(db, session_id, engine.open_trade)
    for trade in engine.closed_trades:
        await _upsert_replay_order(db, session_id, trade)

    await db.commit()


async def reset_replay_session(db: AsyncSession, session_id: uuid.UUID, engine: ReplayEngine) -> None:
    """A reset discards every trade the in-memory engine had — the
    persisted `replay_orders` rows for this session need to go with them,
    or they'd describe trades that no longer exist."""
    await db.execute(delete(ReplayOrder).where(ReplayOrder.replay_session_id == session_id))
    await sync_replay_session(db, session_id, engine)
