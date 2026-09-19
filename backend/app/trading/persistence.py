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
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.text import clip

from app.database.models.strategy import Direction
from app.database.models.instruments import Instrument as InstrumentRow
from app.database.models.trading import ExecutionMode
from app.database.models.trading import Order as OrderRow
from app.database.models.trading import OrderEvent as OrderEventRow
from app.database.models.trading import Position as PositionRow
from app.database.models.trading import Trade as TradeRow
from app.database.models.trading import OrderStatus
from app.trading.order_manager import OrderEventRecord, OrderRecord
from app.trading.position_manager import PositionRecord

# Read off the column rather than restated, so widening the column cannot
# leave a stale number behind here.
_REJECTION_REASON_LIMIT = OrderRow.__table__.c.rejection_reason.type.length


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
            # The broker wrote this one, and never promised a length:
            # `UpstoxBroker._extract_error_message` falls back to the whole
            # response body, which for a proxy 502 is an HTML page.
            # Measured at 789 characters, this insert raised and the
            # rejection vanished -- no order row at all, so no status, no
            # reason, and no ORDER_REJECTED notification for a live order
            # the broker had refused. `broker_order_id` above is
            # deliberately NOT clipped: a shortened id is a wrong id.
            rejection_reason=clip(order.rejection_reason, _REJECTION_REASON_LIMIT),
        )
        db.add(row)
        await db.flush()
        persisted_event_count = 0
    else:
        row.status = order.status
        row.broker_order_id = order.broker_order_id
        row.rejection_reason = clip(order.rejection_reason, _REJECTION_REASON_LIMIT)
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


async def abandon_position_mirrors(
    db: AsyncSession,
    user_id: uuid.UUID,
    execution_mode: ExecutionMode,
    *,
    source_key: str,
) -> int:
    """Marks this source's open `positions` mirrors not-open because the
    engine behind them is gone, not because anything was exited. Returns
    how many rows were retired.

    `persist_position` cannot express this: it derives `is_open` from a
    `PositionRecord`, whose `is_open` is the property `quantity != 0`, so
    there is no way to hand it "flat but never filled".

    Deliberately journals no `Trade`. Nothing was sold at any price -- the
    simulation was discarded -- and `GET /portfolio.total_realized_pnl`
    sums the `trades` journal, so inventing an exit here would put a
    fabricated P&L into the account's realized total. Leaving the rows'
    quantities and prices intact keeps the record of what the abandoned
    session held; only its contribution to open exposure goes away.

    The lookup is keyed by `source_key` rather than by instrument, so the
    caller does not need a live engine to name the instrument -- which is
    the whole point: after a restart the engine is gone and the row it
    wrote is the only remaining trace of it. **This means the key must
    identify one abandonable engine.** `paper:{session_id}` does: a paper
    session holds one symbol and a new session gets a new UUID. A shared
    key like `manual` or `auto` does not, and passing one here would retire
    a whole book.

    `user_id` is part of the lookup, so this can only ever touch rows the
    calling user's own engines wrote.
    """
    rows = (
        (
            await db.execute(
                select(PositionRow).where(
                    PositionRow.user_id == user_id,
                    PositionRow.execution_mode == execution_mode,
                    PositionRow.source_key == source_key,
                    PositionRow.is_open.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.is_open = False
        row.unrealized_pnl = 0.0
    if rows:
        await db.commit()
    return len(rows)


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


# The `positions.source_key` partitions that are the ACCOUNT's own book,
# and so share its balance: the manual/live stack in `app/api/orders.py`
# and the autonomous loop in `app/workers/auto_trade_worker.py`.
#
# `paper:<session_id>` is deliberately absent. A `POST /paper` session is a
# sandbox the user spins up with its OWN `starting_balance`; its positions
# consume none of the account's capital, so counting them toward the
# account's exposure would refuse real trades over simulated ones, and
# feeding the account's real positions into the sandbox would make the
# simulation answer a question nobody asked.
ACCOUNT_BACKED_SOURCE_KEYS: tuple[str, ...] = ("manual", "auto")


async def load_open_position_notionals_elsewhere(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    excluding_source_key: str,
) -> dict[str, float]:
    """`{symbol: signed notional}` for this user's open positions held by
    the other ACCOUNT-BACKED engines -- `ACCOUNT_BACKED_SOURCE_KEYS` minus
    `excluding_source_key`.

    Blueprint §86 calls exposure a **portfolio** quantity -- "Total
    exposure" -- and `max_exposure_pct` is a percentage of the account
    balance. The account has one balance, so the denominator is shared
    even though the book is not.

    It was not shared in practice. `positions.source_key` partitions the
    table so three independent `PositionManager`s stop overwriting each
    other's rows (see `persist_position`), and each engine then rebuilt
    only its own partition and measured exposure against that. Measured
    through the real endpoints and the real `AutoTradeSupervisor`, one
    account on a 100,000 balance with `max_exposure_pct=100`:

        auto positions open   : 3     gross  93,636.36
        manual POST /orders   : 201   exposure_limit recorded True
        manual stack sees     : 1 position, exposure 52,000.00
        TOTAL gross notional  : 145,636.36   = 145.6% of the account

    Each path was under its own limit and the account was half as levered
    again as the limit allows, with the gate recording a pass.

    Signed -- negative for a short -- to match
    `app.risk.portfolio.signed_notionals_excluding`, whose output this is
    merged with: `correlated_exposure` nets, so an `abs()` here would turn
    a hedge held by the other engine into double concentration. Callers
    take `abs()` themselves for the gross figure.

    Deliberately NOT filtered by `execution_mode`: all three writers
    persist as `ExecutionMode.PAPER` when no broker is connected, which is
    every account's default, so filtering on it would drop exactly the
    rows this exists to find. The `float()` casts are for the same reason
    `load_open_positions` needs them -- `Numeric(18, 6)` comes back as
    `Decimal`.
    """
    rows = (
        await db.execute(
            select(PositionRow.quantity, PositionRow.average_price, InstrumentRow.symbol)
            .join(InstrumentRow, InstrumentRow.id == PositionRow.instrument_id)
            .where(
                PositionRow.user_id == user_id,
                PositionRow.source_key.in_(ACCOUNT_BACKED_SOURCE_KEYS),
                PositionRow.source_key != excluding_source_key,
                PositionRow.is_open.is_(True),
            )
        )
    ).all()

    # Summed rather than assigned: two engines can each hold a position in
    # the same symbol, and they net for the same reason a hedge does.
    notionals: dict[str, float] = {}
    for quantity, average_price, symbol in rows:
        notionals[symbol] = notionals.get(symbol, 0.0) + float(quantity) * float(average_price)
    return notionals


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


# How far back `load_recent_orders` rebuilds. A judgement call, stated
# plainly rather than hidden: within one process `OrderManager` never
# forgets a key, but a process lifetime is itself arbitrary, and loading
# an account's entire order history on every stack build grows without
# bound. Every accidental resubmit this dedupe exists to absorb -- a
# double-click, a client retry, a user re-pressing after a restart --
# happens within minutes. A day is also the unit the risk counters
# already work in (`trades_today`). The residual is real and deliberate:
# an identical order resubmitted more than this after the original still
# creates a second order.
ORDER_REHYDRATION_WINDOW = timedelta(hours=24)


async def load_recent_orders(
    db: AsyncSession,
    user_id: uuid.UUID,
    execution_mode: ExecutionMode,
    *,
    since: datetime,
) -> list[OrderRecord]:
    """Rebuild recent orders, so `OrderManager`'s idempotency index
    survives a restart.

    Without this, a restarted process forgets every key it has issued.
    `create_order` then mints a *new* order for an identical resubmit
    while `persist_order` -- which is idempotent on `idempotency_key` and
    correctly assumes one key means one order -- updates the first
    order's row instead of inserting a second. Measured: an identical
    resubmit after a restart filled a second time (position 100 -> 200)
    while the `orders` table still held one row for 100, so the journal
    could not be reconciled against the position it produced.

    Events are restored, not left empty, and that is load-bearing rather
    than tidiness: `persist_order` appends `order.events[n:]` where `n` is
    the count already in the database. An order restored with no events
    would have every subsequent transition fall outside that slice and
    never be written, silently ending the audit trail at the restart.
    """
    rows = (
        await db.execute(
            select(OrderRow, InstrumentRow.symbol)
            .join(InstrumentRow, InstrumentRow.id == OrderRow.instrument_id)
            .where(
                OrderRow.user_id == user_id,
                OrderRow.execution_mode == execution_mode,
                OrderRow.created_at >= since,
            )
        )
    ).all()
    if not rows:
        return []

    events_by_order: dict[uuid.UUID, list[OrderEventRecord]] = {}
    for event in (
        await db.execute(
            select(OrderEventRow)
            .where(OrderEventRow.order_id.in_([row.id for row, _ in rows]))
            .order_by(OrderEventRow.occurred_at, OrderEventRow.created_at)
        )
    ).scalars():
        events_by_order.setdefault(event.order_id, []).append(
            OrderEventRecord(
                from_status=OrderStatus(event.from_status) if event.from_status else None,
                to_status=OrderStatus(event.to_status),
                detail=(event.detail or {}).get("detail", ""),
                occurred_at=event.occurred_at,
            )
        )

    account_id = str(user_id)
    return [
        OrderRecord(
            id=row.id,
            idempotency_key=row.idempotency_key,
            account_id=account_id,
            symbol=symbol,
            direction=row.direction,
            order_type=row.order_type,
            quantity=float(row.quantity),
            price=float(row.price) if row.price is not None else None,
            status=row.status,
            broker_order_id=row.broker_order_id,
            rejection_reason=row.rejection_reason,
            events=events_by_order.get(row.id, []),
        )
        for row, symbol in rows
    ]


# The `journal.source` every `record_trade` row carries. It is what tells
# the manual/live path's trades apart from `/paper/*`'s ("manual_paper")
# and the auto-trade worker's rows, which the counters below must not
# absorb: `execution_mode` cannot do it, because a manual stack with no
# connected broker trades against `MockBroker` and persists as PAPER too
# (see `_execution_mode_for`).
MANUAL_TRADE_SOURCE = "manual_order"


def risk_window_starts(now: datetime) -> tuple[datetime, datetime]:
    """The UTC day and ISO-week boundaries the risk counters are measured
    from -- the same bucketing `_UserTradingStack._roll_risk_window` uses
    to decide when to reset them, expressed as timestamps so the journal
    can be queried over exactly the window the in-memory counter covers.
    """
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return day_start, day_start - timedelta(days=now.weekday())


async def load_orders_placed_since(
    db: AsyncSession, user_id: uuid.UUID, execution_mode: ExecutionMode, *, since: datetime
) -> int:
    """Rebuild `_UserTradingStack.trades_today`.

    `place_order` increments that counter once per order it actually
    creates and submits, and `persist_order` writes exactly one row per
    such order (it is idempotent on `idempotency_key`, so a deduped
    resubmit updates the row rather than adding one, and increments
    nothing either). Counting rows is therefore the same number, not an
    approximation of it. Protective stops do not go through `OrderManager`
    at all and write no row, and nothing outside `POST /orders` and
    `POST /options/execute` writes to `orders`.
    """
    return int(
        (
            await db.execute(
                select(func.count(OrderRow.id)).where(
                    OrderRow.user_id == user_id,
                    OrderRow.execution_mode == execution_mode,
                    OrderRow.created_at >= since,
                )
            )
        ).scalar_one()
    )


async def load_realized_pnl_since(
    db: AsyncSession, user_id: uuid.UUID, execution_mode: ExecutionMode, *, since: datetime
) -> float:
    """Rebuild `_UserTradingStack.daily_pnl`/`weekly_pnl`.

    `place_order` does `stack.daily_pnl += realized_delta` and passes that
    same `realized_delta` to `record_trade` as `pnl`, so summing the
    journal over a window reproduces the counter for that window exactly.
    """
    total = (
        await db.execute(
            select(func.coalesce(func.sum(TradeRow.pnl), 0)).where(
                TradeRow.user_id == user_id,
                TradeRow.execution_mode == execution_mode,
                TradeRow.journal["source"].as_string() == MANUAL_TRADE_SOURCE,
                TradeRow.closed_at >= since,
            )
        )
    ).scalar_one()
    return float(total)


# The auto-trade worker's own `journal.source`, the third value alongside
# `manual_order` and `/paper/*`'s `manual_paper`. Added when the worker's
# risk counters started being rebuilt from the journal: before that no
# reader needed to tell the three writers apart, and the worker was the
# only one leaving `source` unset.
AUTO_TRADE_SOURCE = "auto_trade"


def _written_by_the_auto_trade_worker():
    """Rows the auto-trade worker wrote.

    The second clause covers rows written before `AUTO_TRADE_SOURCE`
    existed. Leaving them out would silently under-count the day a
    deployment lands -- in the unsafe direction, since an unseen loss is a
    loss the limit does not know about. The worker was the only writer
    that left `source` unset, and `manual_paper`/`manual_order` both set
    it, so an unset source plus a strategy attribution is unambiguous.
    """
    source = TradeRow.journal["source"].as_string()
    return or_(
        source == AUTO_TRADE_SOURCE,
        and_(source.is_(None), TradeRow.strategy_id.isnot(None)),
    )


async def load_auto_trade_entries_since(
    db: AsyncSession, user_id: uuid.UUID, *, since: datetime, source_key: str
) -> int:
    """Rebuild the auto-trade `RiskWindow.trades_today`.

    Unlike the manual path, this cannot be one row count. That counter
    moves when an entry *opens*, and this path journals a `trades` row only
    when a position *closes* -- so an entry taken today and still running
    has no trade row at all. It is the sum of two disjoint sets:

    - trades opened today (closed ones, by `opened_at`), and
    - positions opened today that are still open.

    Disjoint because closing a position flips `is_open` to false on the
    same row rather than deleting it, so a round trip contributes its
    trade row and is excluded from the open-position count. A second entry
    on the same instrument the same day gets its own row -- the unique
    index is partial on `is_open` -- so it is counted once too.
    """
    closed_today = (
        await db.execute(
            select(func.count(TradeRow.id)).where(
                TradeRow.user_id == user_id,
                _written_by_the_auto_trade_worker(),
                TradeRow.opened_at >= since,
            )
        )
    ).scalar_one()
    still_open = (
        await db.execute(
            select(func.count(PositionRow.id)).where(
                PositionRow.user_id == user_id,
                PositionRow.source_key == source_key,
                PositionRow.is_open.is_(True),
                PositionRow.created_at >= since,
            )
        )
    ).scalar_one()
    return int(closed_today) + int(still_open)


async def load_auto_trade_realized_pnl_since(
    db: AsyncSession, user_id: uuid.UUID, *, since: datetime
) -> float:
    """Rebuild the auto-trade `RiskWindow.daily_pnl`/`weekly_pnl`. The
    worker writes `pnl=outcome.closed_position_pnl`, the same value it adds
    to the window, so summing the journal reproduces the counter exactly.
    """
    total = (
        await db.execute(
            select(func.coalesce(func.sum(TradeRow.pnl), 0)).where(
                TradeRow.user_id == user_id,
                _written_by_the_auto_trade_worker(),
                TradeRow.closed_at >= since,
            )
        )
    ).scalar_one()
    return float(total)
