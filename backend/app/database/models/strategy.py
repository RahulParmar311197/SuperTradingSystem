import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, updated_at_col, uuid_pk
from app.database.session import Base


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Strategy(Base):
    """A versioned, user-owned strategy definition (Strategy DSL as JSON)."""

    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    eligible_for_auto_trading: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class Signal(Base):
    """A structured detection produced by the SMC/ICT + strategy engine."""

    __tablename__ = "signals"

    id: Mapped[uuid.UUID] = uuid_pk()
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False, index=True
    )
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("strategies.id"), index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    direction: Mapped[Direction] = mapped_column(Enum(Direction, name="signal_direction"), nullable=False)
    bias: Mapped[str | None] = mapped_column(String(16))
    entry: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    stop: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    target: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    risk_reward: Mapped[float] = mapped_column(Numeric(10, 4), nullable=False)
    score: Mapped[float] = mapped_column(Numeric(6, 2), default=0)
    context: Mapped[dict] = mapped_column(JSON, default=dict)  # conditions satisfied/missing etc.
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()


class Setup(Base):
    """A raw SMC/ICT pattern detection (structure break, FVG, order block, etc.)."""

    __tablename__ = "setups"

    id: Mapped[uuid.UUID] = uuid_pk()
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False, index=True
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    setup_type: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()
