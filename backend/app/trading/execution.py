"""Execution engine: takes a risk-approved order and drives it through a
broker adapter, updating the order state machine and position manager as
fills arrive (blueprint §50, §59, §60)."""

from __future__ import annotations

import uuid

from app.brokers.base import Broker, OrderRequest
from app.database.models.trading import OrderStatus
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager


class ExecutionEngine:
    def __init__(self, broker: Broker, order_manager: OrderManager, position_manager: PositionManager) -> None:
        self.broker = broker
        self.order_manager = order_manager
        self.position_manager = position_manager

    async def submit(self, order_id: uuid.UUID) -> None:
        order = self.order_manager.get(order_id)
        if order is None:
            raise KeyError(f"Unknown order {order_id}")
        if order.status != OrderStatus.RISK_APPROVED:
            raise ValueError(f"Order {order_id} must be RISK_APPROVED before submission, is {order.status}")

        self.order_manager.transition(order_id, OrderStatus.SUBMITTED, "sent to broker")

        request = OrderRequest(
            idempotency_key=order.idempotency_key,
            symbol=order.symbol,
            direction=order.direction,
            order_type=order.order_type,
            quantity=order.quantity,
            price=order.price,
        )
        result = await self.broker.place_order(request)
        order.broker_order_id = result.broker_order_id

        if result.status == OrderStatus.REJECTED:
            self.order_manager.transition(order_id, OrderStatus.REJECTED, result.rejection_reason or "rejected by broker")
            return

        self.order_manager.transition(order_id, OrderStatus.ACKNOWLEDGED, "broker acknowledged")

        if result.filled_quantity > 0:
            order.filled_quantity = result.filled_quantity
            order.average_fill_price = result.average_fill_price
            next_status = (
                OrderStatus.FILLED if result.filled_quantity >= order.quantity else OrderStatus.PARTIALLY_FILLED
            )
            self.order_manager.transition(order_id, next_status, "filled by broker")
            self.position_manager.apply_fill(
                order.account_id, order.symbol, order.direction, result.filled_quantity, result.average_fill_price
            )
            if next_status == OrderStatus.FILLED:
                self.order_manager.transition(order_id, OrderStatus.MONITORING, "position open")
