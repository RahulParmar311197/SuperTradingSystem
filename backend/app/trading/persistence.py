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
from app.database.models.instruments import Instrument as InstrumentRow
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
    broker_account_id: uuid.UUID | None = None,
) -> OrderRow:
    """Insert-or-update the DB mirror of `order`, appending any
    `OrderEvent` rows not yet persisted. Safe to call after every state
    transition — idempotent on `order.idempotency_key`.

    `execution_mode` only takes effect when the row is first created (an
    order's execution mode can't change across its own state transitions)
    — callers must pass the same value on every call for a given order.
    `broker_account_id` is `None` for paper/backtest/replay orders, which
    have no connected `BrokerAccount` to attribute to — only
    `app/api/orders.py`'s manual/live path ever has one to pass.
    """
    row = (
        await db.execute(select(OrderRow).where(OrderRow.idempotency_key == order.idempotency_key))
    ).scalar_one_or_none()

    if row is None:
        row = OrderRow(
            id=order.id,
            user_id=user_id,
            instrument_id=instrument_id,
            broker_account_id=broker_account_id,
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


async def abandon_position_mirror(
    db: AsyncSession,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    execution_mode: ExecutionMode,
    *,
    source_key: str,
) -> bool:
    """Marks this source's open `positions` mirror not-open because the
    engine behind it is gone, not because the position was exited. Returns
    whether a row was actually found.

    `persist_position` cannot express this: it derives `is_open` from a
    `PositionRecord`, whose `is_open` is the property `quantity != 0`, so
    there is no way to hand it "flat but never filled".

    Deliberately journals no `Trade`. Nothing was sold at any price -- the
    simulation was discarded -- and `GET /portfolio.total_realized_pnl`
    sums the `trades` journal, so inventing an exit here would put a
    fabricated P&L into the account's realized total. Leaving the row's
    quantity and prices intact keeps the record of what the abandoned
    session held; only its contribution to open exposure goes away.

    The lookup is keyed exactly as `persist_position`'s is, so it can only
    ever retire the row this source itself wrote.
    """
    row = (
        await db.execute(
            select(PositionRow).where(
                PositionRow.user_id == user_id,
                PositionRow.instrument_id == instrument_id,
                PositionRow.execution_mode == execution_mode,
                PositionRow.source_key == source_key,
                PositionRow.is_open.is_(True),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return False

    row.is_open = False
    row.unrealized_pnl = 0.0
    await db.commit()
    return True


async def persist_position(
    db: AsyncSession,
    user_id: uuid.UUID,
    instrument_id: uuid.UUID,
    position: PositionRecord,
    execution_mode: ExecutionMode = ExecutionMode.LIVE,
    *,
    source_key: str,
) -> PositionRow:
    """Upsert the DB mirror of `position` — the single open `positions`
    row for this (user, instrument, execution_mode, source_key), or a
    freshly-closed one if `position` just went flat.

    `source_key` identifies which engine's `PositionManager` this row
    mirrors, and is required rather than defaulted precisely so a new
    caller cannot silently join an existing engine's row. Three unrelated
    managers write here — the manual stack in `app/api/orders.py`, each
    `PaperTradingEngine` behind `POST /paper`, and `AutoTradeSupervisor`
    in the worker process — and all of them persist as
    `ExecutionMode.PAPER` when no broker is connected, which is every
    account's default. Without it in the key they overwrote each other's
    row in place, so `GET /portfolio` reported whichever engine wrote last
    as the account's entire exposure. See `Position.source_key`."""
    row = (
        await db.execute(
            select(PositionRow).where(
                PositionRow.user_id == user_id,
                PositionRow.instrument_id == instrument_id,
                PositionRow.execution_mode == execution_mode,
                PositionRow.source_key == source_key,
                PositionRow.is_open.is_(True),
            )
        )
    ).scalar_one_or_none()

    if row is None:
        row = PositionRow(
            user_id=user_id,
            instrument_id=instrument_id,
            execution_mode=execution_mode,
            source_key=source_key,
            quantity=position.quantity,
            average_price=position.average_price,
            stop=position.stop,
            target=position.target,
            protective_order_id=position.protective_order_id,
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
        row.protective_order_id = position.protective_order_id
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


async def load_open_positions(
    db: AsyncSession,
    user_id: uuid.UUID,
    execution_mode: ExecutionMode,
    *,
    source_key: str,
) -> list[PositionRecord]:
    """Rebuild an engine's in-memory positions from their DB mirror — the
    inverse of `persist_position`, and the reason that mirror exists.

    `OrderManager`/`PositionManager` live in one process's memory. Every
    fill is written here, but nothing ever read it back, so a restart left
    the manager empty while the account's real positions sat in
    `positions`. That is not merely a stale read: `current_exposure`,
    `max_open_positions` and `correlated_exposure` are all computed by
    summing the in-memory book, and `is_reducing` — the exemption that
    lets a position always be closed — is decided by looking the position
    up in it. An empty book makes every one of those read zero, so a
    restarted process would let an account re-take its whole exposure and
    would treat an exit as a fresh entry.

    `source_key` is required for the same reason `persist_position`
    requires it: three unrelated managers mirror into this table and all
    of them default to `ExecutionMode.PAPER`, so loading without it would
    hand one engine another engine's positions.

    Note the explicit `float()` casts. The columns are `Numeric(18, 6)`,
    which SQLAlchemy hands back as `Decimal` regardless of the `Mapped[float]`
    annotation, while `PositionRecord` is float throughout. Without the
    cast the first arithmetic mixing a rehydrated value with a float —
    `apply_fill`'s `average_price * quantity + price * signed_qty`, say —
    raises `TypeError`. `app.risk.portfolio.compute_exposure` already
    casts at its own read of these columns for exactly this reason.
    """
    rows = (
        await db.execute(
            select(PositionRow, InstrumentRow.symbol)
            .join(InstrumentRow, InstrumentRow.id == PositionRow.instrument_id)
            .where(
                PositionRow.user_id == user_id,
                PositionRow.execution_mode == execution_mode,
                PositionRow.source_key == source_key,
                PositionRow.is_open.is_(True),
            )
        )
    ).all()

    account_id = str(user_id)
    return [
        PositionRecord(
            account_id=account_id,
            symbol=symbol,
            quantity=float(row.quantity),
            average_price=float(row.average_price),
            realized_pnl=float(row.realized_pnl),
            unrealized_pnl=float(row.unrealized_pnl),
            stop=float(row.stop) if row.stop is not None else None,
            target=float(row.target) if row.target is not None else None,
            protective_order_id=row.protective_order_id,
        )
        for row, symbol in rows
    ]
