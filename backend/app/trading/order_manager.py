"""Order state machine + idempotency (blueprint §59, §76).

This is a pure in-memory implementation so it can be reused unchanged by
live trading, paper trading, replay, and backtest — each of those simply
wraps it with (or without) a database-backed broker. A persistence layer
can mirror `OrderRecord` into the `orders`/`order_events` tables at the API
boundary without this module needing to know about SQLAlchemy.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType

_ALLOWED_TRANSITIONS: dict[OrderStatus, set[OrderStatus]] = {
    OrderStatus.CREATED: {OrderStatus.VALIDATING, OrderStatus.REJECTED, OrderStatus.FAILED},
    OrderStatus.VALIDATING: {OrderStatus.RISK_APPROVED, OrderStatus.REJECTED},
    OrderStatus.RISK_APPROVED: {OrderStatus.SUBMITTED, OrderStatus.REJECTED, OrderStatus.FAILED},
    OrderStatus.SUBMITTED: {OrderStatus.ACKNOWLEDGED, OrderStatus.REJECTED, OrderStatus.FAILED},
    OrderStatus.ACKNOWLEDGED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.EXPIRED},
    OrderStatus.PARTIALLY_FILLED: {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED},
    OrderStatus.FILLED: {OrderStatus.MONITORING, OrderStatus.CLOSED},
    OrderStatus.MONITORING: {OrderStatus.CLOSED},
    OrderStatus.CLOSED: set(),
    OrderStatus.REJECTED: set(),
    OrderStatus.CANCELLED: set(),
    OrderStatus.EXPIRED: set(),
    OrderStatus.FAILED: set(),
}


class IllegalTransitionError(Exception):
    pass


@dataclass(slots=True)
class OrderEventRecord:
    from_status: OrderStatus | None
    to_status: OrderStatus
    detail: str
    occurred_at: datetime


@dataclass(slots=True)
class OrderRecord:
    id: uuid.UUID
    idempotency_key: str
    account_id: str
    symbol: str
    direction: Direction
    order_type: OrderType
    quantity: float
    price: float | None
    status: OrderStatus = OrderStatus.CREATED
    broker_order_id: str | None = None
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    rejection_reason: str | None = None
    events: list[OrderEventRecord] = field(default_factory=list)


class OrderManager:
    def __init__(self) -> None:
        self._orders: dict[uuid.UUID, OrderRecord] = {}
        self._by_idempotency_key: dict[str, uuid.UUID] = {}

    def create_order(
        self,
        idempotency_key: str,
        account_id: str,
        symbol: str,
        direction: Direction,
        order_type: OrderType,
        quantity: float,
        price: float | None = None,
    ) -> tuple[OrderRecord, bool]:
        """Returns (order, created). `created` is False when the idempotency
        key was already seen — the existing order is returned unchanged."""
        existing_id = self._by_idempotency_key.get(idempotency_key)
        if existing_id is not None:
            return self._orders[existing_id], False

        order = OrderRecord(
            id=uuid.uuid4(),
            idempotency_key=idempotency_key,
            account_id=account_id,
            symbol=symbol,
            direction=direction,
            order_type=order_type,
            quantity=quantity,
            price=price,
        )
        order.events.append(OrderEventRecord(None, OrderStatus.CREATED, "created", datetime.now(timezone.utc)))
        self._orders[order.id] = order
        self._by_idempotency_key[idempotency_key] = order.id
        return order, True

    def get(self, order_id: uuid.UUID) -> OrderRecord | None:
        return self._orders.get(order_id)

    def list_all(self, account_id: str | None = None) -> list[OrderRecord]:
        orders = list(self._orders.values())
        if account_id is not None:
            orders = [o for o in orders if o.account_id == account_id]
        return orders

    def transition(self, order_id: uuid.UUID, to_status: OrderStatus, detail: str = "") -> OrderRecord:
        order = self._orders.get(order_id)
        if order is None:
            raise KeyError(f"Unknown order {order_id}")

        allowed = _ALLOWED_TRANSITIONS.get(order.status, set())
        if to_status not in allowed:
            raise IllegalTransitionError(f"Cannot move order from {order.status} to {to_status}")

        order.events.append(OrderEventRecord(order.status, to_status, detail, datetime.now(timezone.utc)))
        order.status = to_status
        if to_status == OrderStatus.REJECTED:
            order.rejection_reason = detail
        return order
