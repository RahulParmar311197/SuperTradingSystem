import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, Enum, ForeignKey, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, uuid_pk
from app.database.session import Base


class RiskDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"


class RiskEvent(Base):
    __tablename__ = "risk_events"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    decision: Mapped[RiskDecision] = mapped_column(Enum(RiskDecision, name="risk_decision"), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    checks: Mapped[dict] = mapped_column(JSON, default=dict)
    related_order_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    created_at: Mapped[datetime] = created_at_col()


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    actor: Mapped[str] = mapped_column(String(64), nullable=False)  # user | system | ai
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()
