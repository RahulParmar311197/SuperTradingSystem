"""Mirrors the in-memory order/position state (`OrderManager`,
`PositionManager`) into Postgres at the API boundary (blueprint §9-13,
§59-61), exactly as `order_manager.py`'s module docstring anticipates.

Without this, orders placed through `POST /orders` only ever existed in
one API process's memory (see `app/api/orders.py`'s `_STACKS`) — gone on
restart, invisible to the reconciliation worker, the admin dashboard, and
any portfolio-risk reporting, and never producing a trade journal entry
(§61) the way autonomous trading already does. This module makes the
manual/live path persist the same way.

The in-memory managers stay the source of truth for *live process* state
(order state machine transitions, position math) — this module only
mirrors their result into the database after each transition, matching
how `AutoTradeSupervisor` already persists `Trade` rows.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models.strategy import Direction
from app.database.models.trading import ExecutionMode
from app.database.models.trading import Order as OrderRow
from app.database.models.trading import OrderEvent as OrderEventRow
from app.database.models.trading import Position as PositionRow
from app.database.models.trading import Trade as TradeRow
from app.trading.order_manager import OrderRecord
from app.trading.position_manager import PositionRecord


async def persist_order(
    db: AsyncSession,
    order: OrderRecord,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    strategy_id: uuid.UUID | None = None,
    strategy_version: int | None = None,
    execution_mode: ExecutionMode = ExecutionMode.LIVE,
) -> OrderRow:
    """Insert-or-update the DB mirror of `order`, appending any
    `OrderEvent` rows not yet persisted. Safe to call after every state
    transition — idempotent on `order.idempotency_key`.

    `execution_mode` only takes effect when the row is first created (an
    order's execution mode can't change across its own state transitions)
    — callers must pass the same value on every call for a given order.
    """
    row = (
        await db.execute(select(OrderRow).where(OrderRow.idempotency_key == order.idempotency_key))
    ).scalar_one_or_none()

    if row is None:
        row = OrderRow(
            id=order.id,
            user_id=user_id,
            instrument_id=instrument_id,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            idempotency_key=order.idempotency_key,
            execution_mode=execution_mode,
            direction=order.direction,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
            status=order.status,
            broker_order_id=order.broker_order_id,
            rejection_reason=order.rejection_reason,
        )
        db.add(row)
        await db.flush()
        persisted_event_count = 0
    else:
        row.status = order.status
        row.broker_order_id = order.broker_order_id
        row.rejection_reason = order.rejection_reason
        persisted_event_count = (
            await db.execute(
                select(func.count()).select_from(OrderEventRow).where(OrderEventRow.order_id == row.id)
            )
        ).scalar_one()

    for event in order.events[persisted_event_count:]:
        db.add(
            OrderEventRow(
                order_id=row.id,
                from_status=event.from_status.value if event.from_status else None,
                to_status=event.to_status.value,
                detail={"detail": event.detail} if event.detail else {},
                occurred_at=event.occurred_at,
            )
        )

    await db.commit()
    await db.refresh(row)
    return row


async def persist_position(
    db: AsyncSession,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    position: PositionRecord,
    execution_mode: ExecutionMode = ExecutionMode.LIVE,
) -> PositionRow:
    """Upsert the DB mirror of `position` — the single open `positions`
    row for this (user, instrument, execution_mode), or a freshly-closed
    one if `position` just went flat."""
    row = (
        await db.execute(
            select(PositionRow).where(
                PositionRow.user_id == user_id,
                PositionRow.instrument_id == instrument_id,
                PositionRow.execution_mode == execution_mode,
                PositionRow.is_open.is_(True),
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = PositionRow(
            user_id=user_id,
            instrument_id=instrument_id,
            execution_mode=execution_mode,
            quantity=position.quantity,
            average_price=position.average_price,
            stop=position.stop,
            target=position.target,
            unrealized_pnl=position.unrealized_pnl,
            realized_pnl=position.realized_pnl,
            is_open=position.is_open,
        )
        db.add(row)
    else:
        row.quantity = position.quantity
        row.average_price = position.average_price
        row.stop = position.stop
        row.target = position.target
        row.unrealized_pnl = position.unrealized_pnl
        row.realized_pnl = position.realized_pnl
        row.is_open = position.is_open

    await db.commit()
    await db.refresh(row)
    return row


async def record_trade(
    db: AsyncSession,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    direction: Direction,
    entry_price: float,
    exit_price: float,
    quantity: float,
    pnl: float,
    stop: float | None = None,
    target: float | None = None,
    position_id: uuid.UUID | None = None,
    strategy_id: uuid.UUID | None = None,
    strategy_version: int | None = None,
    execution_mode: ExecutionMode = ExecutionMode.LIVE,
) -> TradeRow:
    """Write a `trades` journal row (blueprint §61) for a fill that
    realized P&L — i.e. closed or reduced an open position. The caller
    (which holds the position snapshot from before the fill) decides
    when that happened; this just persists the result."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    row = TradeRow(
        user_id=user_id,
        position_id=position_id,
        instrument_id=instrument_id,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        execution_mode=execution_mode,
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=quantity,
        stop=stop,
        target=target,
        pnl=pnl,
        opened_at=now,
        closed_at=now,
        journal={"source": "manual_order"},
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row
