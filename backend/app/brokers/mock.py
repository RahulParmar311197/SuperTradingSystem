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
        # Stop orders that are live at the broker but not yet triggered,
        # keyed by broker_order_id. A real broker holds these; without
        # somewhere to hold them here, a protective stop could not be
        # simulated at all (see place_order).
        self._resting: dict[str, OrderRequest] = {}
        self._positions: dict[str, BrokerPosition] = {}
        self._quotes: dict[str, Quote] = {}
        self._healthy = True

    def set_quote(self, symbol: str, ltp: float, bid: float | None = None, ask: float | None = None) -> None:
        self._quotes[symbol] = Quote(symbol=symbol, ltp=ltp, bid=bid, ask=ask, timestamp=datetime.now(timezone.utc))
        self._trigger_resting_orders(symbol, ltp)

    def _trigger_resting_orders(self, symbol: str, ltp: float) -> None:
        """Fill any resting stop whose trigger this price has crossed.

        A real broker watches the tape; the only "tape" this broker has is
        `set_quote`, so that is where a stop gets its chance to fire. A
        sell-stop (SHORT, protecting a long) triggers at or below its
        trigger price; a buy-stop (LONG, protecting a short) at or above.
        """
        for broker_order_id, request in list(self._resting.items()):
            if request.symbol != symbol or request.trigger_price is None:
                continue
            crossed = (
                ltp <= request.trigger_price
                if request.direction.value == "SHORT"
                else ltp >= request.trigger_price
            )
            if not crossed:
                continue
            del self._resting[broker_order_id]
            # SL_M becomes a market order at the price that triggered it;
            # SL becomes a limit order at its own limit price. Neither can
            # fill better than the trigger it just crossed.
            fill_price = request.price if request.order_type == OrderType.SL else ltp
            if not fill_price or fill_price <= 0:
                fill_price = ltp
            order = self._orders[broker_order_id]
            order.status = OrderStatus.FILLED
            order.filled_quantity = request.quantity
            order.average_fill_price = fill_price
            order.updated_at = datetime.now(timezone.utc)
            self._apply_fill_to_position(request.symbol, request.direction, request.quantity, fill_price)

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

        if request.order_type in (OrderType.SL, OrderType.SL_M) and request.trigger_price is not None:
            # A stop order does not fill on arrival -- it rests at the
            # broker until price crosses its trigger. Before this, SL/SL_M
            # went through `_resolve_fill_price`, which reads `request.price`
            # for any non-MARKET type: an SL_M (market-once-triggered, and
            # so carrying no limit price) resolved to None and came back
            # REJECTED, making a protective stop unplaceable through the
            # only broker any test or paper account actually uses.
            order = BrokerOrder(
                broker_order_id=broker_order_id,
                symbol=request.symbol,
                direction=request.direction,
                order_type=request.order_type,
                quantity=request.quantity,
                price=request.price,
                status=OrderStatus.ACKNOWLEDGED,
                updated_at=datetime.now(timezone.utc),
            )
            self._orders[broker_order_id] = order
            self._resting[broker_order_id] = request
            # A stop placed on the wrong side of the market is already
            # through its trigger and fires immediately, exactly as it
            # would at a real broker.
            quote = self._quotes.get(request.symbol)
            if quote is not None:
                self._trigger_resting_orders(request.symbol, quote.ltp)
            filled = self._orders[broker_order_id]
            return OrderResult(
                broker_order_id=broker_order_id,
                status=filled.status,
                filled_quantity=filled.filled_quantity,
                average_fill_price=filled.average_fill_price,
            )

        fill_price = self._resolve_fill_price(request)
        if fill_price is None:
            reason = f"No usable price for a {request.order_type.value} order on {request.symbol}"
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
            return OrderResult(broker_order_id, OrderStatus.REJECTED, rejection_reason=reason)

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

    def _resolve_fill_price(self, request: OrderRequest) -> float | None:
        """The price this order fills at, or None when there isn't one.

        Returning None fails the order closed rather than inventing a
        fill the market never offered. `request.price or 0.0` used to
        resolve a LIMIT order carrying no limit price to **0.0**, and a
        fill at zero is not a cheap fill -- it corrupts the position's
        average price, reports the position as zero exposure, and turns
        its eventual close into a fabricated profit. A caller that cannot
        say what price it wants gets a rejection it can see, not a
        position it cannot explain.
        """
        if request.order_type == OrderType.MARKET:
            quote = self._quotes.get(request.symbol)
            base_price = quote.ltp if quote else request.price
        else:
            base_price = request.price
        if not base_price or base_price <= 0:
            return None
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
        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED):
            # Already terminal -- cancelling a filled stop must not report
            # and must never un-fill it.
            return OrderResult(broker_order_id, order.status, order.filled_quantity, order.average_fill_price)
        self._resting.pop(broker_order_id, None)
        order.status = OrderStatus.CANCELLED
        return OrderResult(broker_order_id, OrderStatus.CANCELLED)

    async def is_healthy(self) -> bool:
        return self._healthy
