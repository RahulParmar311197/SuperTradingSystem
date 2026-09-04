import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, Enum, ForeignKey, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, uuid_pk
from app.database.session import Base


class NotificationType(StrEnum):
    SETUP_DETECTED = "SETUP_DETECTED"
    TRADE_EXECUTED = "TRADE_EXECUTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    POSITION_CLOSED = "POSITION_CLOSED"
    SL_HIT = "SL_HIT"
    TP_HIT = "TP_HIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    BROKER_DISCONNECTED = "BROKER_DISCONNECTED"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    AUTO_TRADING_DISABLED = "AUTO_TRADING_DISABLED"


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    type: Mapped[NotificationType] = mapped_column(Enum(NotificationType, name="notification_type"), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(String(1000), nullable=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = created_at_col()
