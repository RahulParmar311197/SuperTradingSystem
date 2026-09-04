"""In-memory broker used for paper trading, backtesting, and tests. It
mimics fills/rejections without touching any real market — see blueprint
§49 "Paper Trading" and §108 "Testing"."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from app.brokers.base import (
    AccountInfo,
    Broker,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    Quote,
)
from app.database.models.trading import OrderStatus, OrderType


class MockBroker(Broker):
    def __init__(
        self,
        starting_balance: float = 100_000.0,
        slippage_pct: float = 0.0,
        reject_probability: float = 0.0,
        partial_fill_probability: float = 0.0,
    ) -> None:
        self._balance = starting_balance
        self._equity = starting_balance
        self.slippage_pct = slippage_pct
        self.reject_probability = reject_probability
        self.partial_fill_probability = partial_fill_probability
        self._orders: dict[str, BrokerOrder] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._quotes: dict[str, Quote] = {}
        self._healthy = True

    def set_quote(self, symbol: str, ltp: float, bid: float | None = None, ask: float | None = None) -> None:
        self._quotes[symbol] = Quote(symbol=symbol, ltp=ltp, bid=bid, ask=ask, timestamp=datetime.now(timezone.utc))

    def set_healthy(self, healthy: bool) -> None:
        self._healthy = healthy

    async def get_account(self) -> AccountInfo:
        return AccountInfo(account_id="MOCK", balance=self._balance, equity=self._equity)

    async def get_positions(self) -> list[BrokerPosition]:
        return list(self._positions.values())

    async def get_orders(self) -> list[BrokerOrder]:
        return list(self._orders.values())

    async def get_quote(self, symbol: str) -> Quote:
        if symbol not in self._quotes:
            raise KeyError(f"No mock quote set for {symbol}")
        return self._quotes[symbol]

    async def place_order(self, request: OrderRequest) -> OrderResult:
        broker_order_id = str(uuid.uuid4())

        if self.reject_probability > 0:
            import random

            if random.random() < self.reject_probability:
                order = BrokerOrder(
                    broker_order_id=broker_order_id,
                    symbol=request.symbol,
                    direction=request.direction,
                    order_type=request.order_type,
                    quantity=request.quantity,
                    price=request.price,
                    status=OrderStatus.REJECTED,
                    updated_at=datetime.now(timezone.utc),
                )
                self._orders[broker_order_id] = order
                return OrderResult(broker_order_id, OrderStatus.REJECTED, rejection_reason="Simulated rejection")

        fill_price = self._resolve_fill_price(request)

        filled_quantity = request.quantity
        if self.partial_fill_probability > 0:
            import random

            if random.random() < self.partial_fill_probability:
                filled_quantity = round(request.quantity * random.uniform(0.3, 0.9), 6)

        status = OrderStatus.FILLED if filled_quantity >= request.quantity else OrderStatus.PARTIALLY_FILLED
        order = BrokerOrder(
            broker_order_id=broker_order_id,
            symbol=request.symbol,
            direction=request.direction,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            status=status,
            filled_quantity=filled_quantity,
            average_fill_price=fill_price,
            updated_at=datetime.now(timezone.utc),
        )
        self._orders[broker_order_id] = order
        self._apply_fill_to_position(request.symbol, request.direction, filled_quantity, fill_price)

        return OrderResult(
            broker_order_id=broker_order_id,
            status=status,
            filled_quantity=filled_quantity,
            average_fill_price=fill_price,
        )

    def _resolve_fill_price(self, request: OrderRequest) -> float:
        if request.order_type == OrderType.MARKET:
            quote = self._quotes.get(request.symbol)
            base_price = quote.ltp if quote else (request.price or 0.0)
        else:
            base_price = request.price or 0.0
        slippage = base_price * (self.slippage_pct / 100)
        return base_price + slippage if request.direction.value == "LONG" else base_price - slippage

    def _apply_fill_to_position(self, symbol: str, direction, quantity: float, price: float) -> None:
        signed_qty = quantity if direction.value == "LONG" else -quantity
        position = self._positions.get(symbol)
        if position is None:
            self._positions[symbol] = BrokerPosition(symbol=symbol, quantity=signed_qty, average_price=price)
            return

        new_quantity = position.quantity + signed_qty
        if new_quantity == 0:
            del self._positions[symbol]
            return
        if (position.quantity > 0) == (signed_qty > 0):
            total_cost = position.average_price * position.quantity + price * signed_qty
            position.average_price = total_cost / new_quantity
        position.quantity = new_quantity

    async def modify_order(self, broker_order_id: str, **changes) -> OrderResult:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise KeyError(f"Unknown order {broker_order_id}")
        for key, value in changes.items():
            if hasattr(order, key):
                setattr(order, key, value)
        return OrderResult(broker_order_id, order.status, order.filled_quantity, order.average_fill_price)

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        order = self._orders.get(broker_order_id)
        if order is None:
            raise KeyError(f"Unknown order {broker_order_id}")
        order.status = OrderStatus.CANCELLED
        return OrderResult(broker_order_id, OrderStatus.CANCELLED)

    async def is_healthy(self) -> bool:
        return self._healthy
