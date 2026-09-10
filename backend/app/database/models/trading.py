import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, JSON, Numeric, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, updated_at_col, uuid_pk
from app.database.models.strategy import Direction
from app.database.session import Base


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    MONITORING = "MONITORING"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SL_M = "SL_M"


class ExecutionMode(StrEnum):
    LIVE = "LIVE"
    PAPER = "PAPER"
    BACKTEST = "BACKTEST"
    REPLAY = "REPLAY"


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False, index=True
    )
    broker_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("broker_accounts.id")
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id")
    )
    # Strategy.version at the moment this order was created — Strategy rows
    # are mutated in place on update (see app/api/strategies.py), so without
    # this an order's strategy_id alone can't say which definition produced
    # it (blueprint §91: "a live trading account should always know exactly
    # which version created a trade").
    strategy_version: Mapped[int | None] = mapped_column(Integer)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("signals.id"))

    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        Enum(ExecutionMode, name="execution_mode"), nullable=False
    )
    direction: Mapped[Direction] = mapped_column(Enum(Direction, name="order_direction"), nullable=False)
    order_type: Mapped[OrderType] = mapped_column(Enum(OrderType, name="order_type"), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    price: Mapped[float | None] = mapped_column(Numeric(18, 6))
    trigger_price: Mapped[float | None] = mapped_column(Numeric(18, 6))
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus, name="order_status"), default=OrderStatus.CREATED, nullable=False
    )
    broker_order_id: Mapped[str | None] = mapped_column(String(128))
    rejection_reason: Mapped[str | None] = mapped_column(String(500))

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class OrderEvent(Base):
    __tablename__ = "order_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False, index=True
    )
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        Enum(ExecutionMode, name="position_execution_mode"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    average_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False, default=0)
    stop: Mapped[float | None] = mapped_column(Numeric(18, 6))
    target: Mapped[float | None] = mapped_column(Numeric(18, 6))
    unrealized_pnl: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    realized_pnl: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    is_open: Mapped[bool] = mapped_column(default=True)
    # Which independent engine owns this row. `positions` is a DB mirror of
    # an in-memory `PositionManager`, and there are three unrelated ones
    # writing here: the manual/live stack in `app/api/orders.py`'s
    # `_STACKS`, each `PaperTradingEngine` behind `POST /paper`, and
    # `AutoTradeSupervisor` in the worker process. They share no state --
    # they cannot, since the supervisor runs in a different process -- and
    # every one of them persists under `ExecutionMode.PAPER` whenever no
    # broker is connected, which is every account's default.
    #
    # Without this column the lookup key was (user, instrument,
    # execution_mode, is_open), so whichever engine wrote last silently
    # overwrote the others' row: a paper session holding 151.5 units and a
    # manual order holding 100 became one row reading 100, and
    # GET /portfolio reported that as the account's whole exposure.
    #
    # "manual" for POST /orders and POST /options/execute, "auto" for the
    # supervisor, "paper:{session_id}" per paper session.
    source_key: Mapped[str] = mapped_column(String(72), nullable=False, server_default="manual")

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        Index(
            "uq_open_position_per_source",
            "user_id",
            "instrument_id",
            "execution_mode",
            "source_key",
            unique=True,
            postgresql_where=text("is_open"),
        ),
    )


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("positions.id"))
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False, index=True
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("strategies.id"))
    strategy_version: Mapped[int | None] = mapped_column(Integer)
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        Enum(ExecutionMode, name="trade_execution_mode"), nullable=False
    )
    direction: Mapped[Direction] = mapped_column(Enum(Direction, name="trade_direction"), nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 6))
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    stop: Mapped[float | None] = mapped_column(Numeric(18, 6))
    target: Mapped[float | None] = mapped_column(Numeric(18, 6))
    pnl: Mapped[float | None] = mapped_column(Numeric(18, 6))
    r_multiple: Mapped[float | None] = mapped_column(Numeric(10, 4))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    journal: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = created_at_col()


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        Enum(ExecutionMode, name="portfolio_execution_mode"), nullable=False
    )
    balance: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    equity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    total_exposure: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    net_delta: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    net_gamma: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    net_theta: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    net_vega: Mapped[float] = mapped_column(Numeric(18, 6), default=0)
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()
