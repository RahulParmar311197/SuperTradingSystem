import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, uuid_pk
from app.database.session import Base


class BacktestStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Backtest(Base):
    __tablename__ = "backtests"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    strategy_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), nullable=False
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    starting_capital: Mapped[float] = mapped_column(Numeric(18, 6), default=100000)
    cost_model: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[BacktestStatus] = mapped_column(
        Enum(BacktestStatus, name="backtest_status"), default=BacktestStatus.QUEUED
    )

    created_at: Mapped[datetime] = created_at_col()


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id: Mapped[uuid.UUID] = uuid_pk()
    backtest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtests.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    exit_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    pnl: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    r_multiple: Mapped[float | None] = mapped_column(Numeric(10, 4))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()


class BacktestMetrics(Base):
    __tablename__ = "backtest_metrics"

    id: Mapped[uuid.UUID] = uuid_pk()
    backtest_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("backtests.id"), nullable=False, unique=True
    )
    total_return: Mapped[float] = mapped_column(Numeric(18, 6))
    net_profit: Mapped[float] = mapped_column(Numeric(18, 6))
    win_rate: Mapped[float] = mapped_column(Numeric(6, 4))
    profit_factor: Mapped[float | None] = mapped_column(Numeric(10, 4))
    expectancy: Mapped[float | None] = mapped_column(Numeric(18, 6))
    max_drawdown: Mapped[float] = mapped_column(Numeric(18, 6))
    sharpe: Mapped[float | None] = mapped_column(Numeric(10, 4))
    sortino: Mapped[float | None] = mapped_column(Numeric(10, 4))
    average_win: Mapped[float | None] = mapped_column(Numeric(18, 6))
    average_loss: Mapped[float | None] = mapped_column(Numeric(18, 6))
    average_r: Mapped[float | None] = mapped_column(Numeric(10, 4))
    total_trades: Mapped[int] = mapped_column(default=0)
    long_trades: Mapped[int] = mapped_column(default=0)
    short_trades: Mapped[int] = mapped_column(default=0)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)
    drawdown_curve: Mapped[list] = mapped_column(JSON, default=list)
    monthly_returns: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = created_at_col()
