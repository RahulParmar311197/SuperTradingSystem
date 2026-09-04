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


def reconcile_orders(local_orders: list[OrderRecord], broker_orders: list[BrokerOrder]) -> list[str]:
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

    local_broker_ids = {o.broker_order_id for o in local_orders if o.broker_order_id}
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
    return ReconciliationReport(
        order_mismatches=reconcile_orders(local_orders, broker_orders),
        position_mismatches=reconcile_positions(local_positions, broker_positions),
    )
