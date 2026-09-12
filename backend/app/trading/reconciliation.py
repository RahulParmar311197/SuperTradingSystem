"""Order/position reconciliation (blueprint §75): after a disconnect, or
just periodically, local state must never be trusted blindly against what
the broker actually holds. A mismatch here is not itself the fix — it's
the trigger for an operator/worker to investigate before resuming new
entries (blueprint §74 "Never assume the local position state is correct
after a disconnect")."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.brokers.base import BrokerOrder, BrokerPosition
from app.database.models.trading import OrderStatus
from app.trading.order_manager import OrderRecord
from app.trading.position_manager import PositionRecord

_OPEN_ORDER_STATUSES = {OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}


@dataclass(slots=True)
class ReconciliationReport:
    order_mismatches: list[str] = field(default_factory=list)
    position_mismatches: list[str] = field(default_factory=list)

    @property
    def in_sync(self) -> bool:
        return not self.order_mismatches and not self.position_mismatches


def reconcile_orders(
    local_orders: list[OrderRecord],
    broker_orders: list[BrokerOrder],
    protective_order_ids: frozenset[str] = frozenset(),
) -> list[str]:
    """`protective_order_ids` are broker orders this system placed that
    deliberately have no `OrderRecord`.

    `app/trading/protective_stops.py` places a position's resting stop by
    calling `broker.place_order` directly rather than going through
    `OrderManager`: it is not an order anyone submitted, it carries no
    idempotency key of its own, and its lifecycle is the position's, not
    the order journal's. The position holds the only reference to it, in
    `PositionRecord.protective_order_id`.

    Without this exemption the unknown-order check below flagged every one
    of them. A resting stop reports ACKNOWLEDGED, which is in the set that
    check treats as a live order nobody knows about, so
    `ReconciliationWorker` halted the account and raised
    RECONCILIATION_REQUIRED within 60 seconds of *every* live entry that
    carried a stop -- and resuming is a deliberate manual admin action.
    The stop-loss feature and the reconciliation loop, each correct alone,
    between them made live trading unusable.

    The exemption is exactly these ids and nothing else: a broker order
    this system did not place is still a mismatch, and so is a stop whose
    position no longer references it. Nor does it hide a stop that
    *fired* -- that shows up in `reconcile_positions` as the position
    being open locally and gone (or reversed) at the broker, which is the
    consequence that actually matters.
    """
    mismatches: list[str] = []
    broker_by_id = {o.broker_order_id: o for o in broker_orders}

    for local in local_orders:
        if local.status not in _OPEN_ORDER_STATUSES:
            continue
        if local.broker_order_id is None:
            mismatches.append(f"Order {local.id} is {local.status.value} locally but was never submitted to the broker")
            continue
        broker_order = broker_by_id.get(local.broker_order_id)
        if broker_order is None:
            mismatches.append(f"Order {local.id} ({local.broker_order_id}) not found at broker")
        elif broker_order.status != local.status:
            mismatches.append(
                f"Order {local.id} ({local.broker_order_id}) status mismatch: "
                f"local={local.status.value} broker={broker_order.status.value}"
            )
        elif abs(broker_order.filled_quantity - local.filled_quantity) > 1e-9:
            mismatches.append(
                f"Order {local.id} ({local.broker_order_id}) filled quantity mismatch: "
                f"local={local.filled_quantity} broker={broker_order.filled_quantity}"
            )

    local_broker_ids = {o.broker_order_id for o in local_orders if o.broker_order_id} | set(protective_order_ids)
    for broker_order in broker_orders:
        if broker_order.broker_order_id not in local_broker_ids and broker_order.status in (
            OrderStatus.SUBMITTED,
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        ):
            mismatches.append(f"Broker order {broker_order.broker_order_id} for {broker_order.symbol} is unknown locally")

    return mismatches


def reconcile_positions(local_positions: list[PositionRecord], broker_positions: list[BrokerPosition]) -> list[str]:
    mismatches: list[str] = []
    broker_by_symbol = {p.symbol: p for p in broker_positions}
    local_by_symbol = {p.symbol: p for p in local_positions if p.is_open}

    for symbol, local in local_by_symbol.items():
        broker_position = broker_by_symbol.get(symbol)
        if broker_position is None:
            mismatches.append(f"{symbol}: open locally (qty={local.quantity}) but no position at broker")
        elif abs(broker_position.quantity - local.quantity) > 1e-9:
            mismatches.append(f"{symbol}: quantity mismatch local={local.quantity} broker={broker_position.quantity}")

    for symbol, broker_position in broker_by_symbol.items():
        if symbol not in local_by_symbol and broker_position.quantity != 0:
            mismatches.append(f"{symbol}: open at broker (qty={broker_position.quantity}) but not tracked locally")

    return mismatches


def reconcile(
    local_orders: list[OrderRecord],
    broker_orders: list[BrokerOrder],
    local_positions: list[PositionRecord],
    broker_positions: list[BrokerPosition],
) -> ReconciliationReport:
    # The resting protective stops belong to the positions, not to the
    # order journal -- see `reconcile_orders`. Taken from every local
    # position, not just the open ones: a position that has just gone flat
    # can still be holding the id of a stop the broker has not finished
    # retiring.
    protective_order_ids = frozenset(
        position.protective_order_id for position in local_positions if position.protective_order_id
    )
    return ReconciliationReport(
        order_mismatches=reconcile_orders(local_orders, broker_orders, protective_order_ids),
        position_mismatches=reconcile_positions(local_positions, broker_positions),
    )
