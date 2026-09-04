import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, uuid_pk
from app.database.session import Base


class ReplayStatus(StrEnum):
    CREATED = "CREATED"
    PLAYING = "PLAYING"
    PAUSED = "PAUSED"
    FINISHED = "FINISHED"


class ReplaySession(Base):
    __tablename__ = "replay_sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False
    )
    timeframe: Mapped[str] = mapped_column(String(8), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    current_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    speed: Mapped[float] = mapped_column(Numeric(6, 2), default=1.0)
    status: Mapped[ReplayStatus] = mapped_column(
        Enum(ReplayStatus, name="replay_status"), default=ReplayStatus.CREATED
    )
    starting_balance: Mapped[float] = mapped_column(Numeric(18, 6), default=100000)
    balance: Mapped[float] = mapped_column(Numeric(18, 6), default=100000)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = created_at_col()


class ReplayOrder(Base):
    __tablename__ = "replay_orders"

    id: Mapped[uuid.UUID] = uuid_pk()
    replay_session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("replay_sessions.id"), nullable=False, index=True
    )
    direction: Mapped[str] = mapped_column(String(8), nullable=False)
    entry_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    exit_price: Mapped[float | None] = mapped_column(Numeric(18, 6))
    stop: Mapped[float | None] = mapped_column(Numeric(18, 6))
    target: Mapped[float | None] = mapped_column(Numeric(18, 6))
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    pnl: Mapped[float | None] = mapped_column(Numeric(18, 6))
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = created_at_col()
