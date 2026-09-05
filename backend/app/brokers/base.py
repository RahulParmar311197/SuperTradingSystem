"""Broker abstraction (blueprint §50, §53).

The strategy/risk/execution layers must never branch on which broker is
connected — everything speaks this interface, and a concrete adapter
(`DhanBroker`, `UpstoxBroker`, `MockBroker`, ...) is selected once, at the
edge, by the account's configured broker.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from app.database.models.trading import OrderStatus, OrderType
from app.database.models.strategy import Direction


@dataclass(slots=True)
class AccountInfo:
    account_id: str
    balance: float
    equity: float
    margin_used: float = 0.0
    margin_available: float = 0.0


@dataclass(slots=True)
class BrokerPosition:
    symbol: str
    quantity: float
    average_price: float
    unrealized_pnl: float = 0.0


@dataclass(slots=True)
class BrokerOrder:
    broker_order_id: str
    symbol: str
    direction: Direction
    order_type: OrderType
    quantity: float
    price: float | None
    status: OrderStatus
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    updated_at: datetime | None = None


@dataclass(slots=True)
class Quote:
    symbol: str
    ltp: float
    bid: float | None = None
    ask: float | None = None
    timestamp: datetime | None = None


@dataclass(slots=True)
class OrderRequest:
    idempotency_key: str
    symbol: str
    direction: Direction
    order_type: OrderType
    quantity: float
    price: float | None = None
    trigger_price: float | None = None


@dataclass(slots=True)
class OrderResult:
    broker_order_id: str
    status: OrderStatus
    filled_quantity: float = 0.0
    average_fill_price: float | None = None
    rejection_reason: str | None = None


class BrokerError(Exception):
    pass


class Broker(ABC):
    """Every broker adapter (live or simulated) implements this contract."""

    @abstractmethod
    async def get_account(self) -> AccountInfo: ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_orders(self) -> list[BrokerOrder]: ...

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> OrderResult:
        """A broker-level rejection (bad margin, closed market, invalid
        instrument, ...) must always be returned as
        `OrderResult(status=OrderStatus.REJECTED, rejection_reason=...)`,
        never raised as `BrokerError` or any other exception -- callers
        (`ExecutionEngine.submit`) have no try/except around this call, and
        the caller has already registered the order under its idempotency
        key before calling, so an uncaught exception here both 500s the
        request and permanently wedges the order (a retry with the same
        order params short-circuits on the idempotency key and never calls
        this method again). This applies equally to a rejection surfaced as
        an HTTP 4xx/5xx *and* one surfaced as an HTTP 200 with an
        error-shaped body -- catch both."""
        ...

    @abstractmethod
    async def modify_order(self, broker_order_id: str, **changes) -> OrderResult: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> OrderResult: ...

    @abstractmethod
    async def is_healthy(self) -> bool: ...
