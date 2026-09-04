import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Enum, ForeignKey, JSON, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, uuid_pk
from app.database.session import Base


class AIDecisionType(StrEnum):
    ANALYSIS = "ANALYSIS"
    TRADE_PROPOSAL = "TRADE_PROPOSAL"
    STRATEGY_BUILD = "STRATEGY_BUILD"
    EXPLANATION = "EXPLANATION"


class AIDecision(Base):
    __tablename__ = "ai_decisions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    decision_type: Mapped[AIDecisionType] = mapped_column(
        Enum(AIDecisionType, name="ai_decision_type"), nullable=False
    )
    input_context: Mapped[dict] = mapped_column(JSON, nullable=False)
    output: Mapped[dict] = mapped_column(JSON, nullable=False)
    validated: Mapped[bool] = mapped_column(default=False)
    validation_errors: Mapped[list] = mapped_column(JSON, default=list)
    model: Mapped[str | None] = mapped_column(String(128))

    created_at: Mapped[datetime] = created_at_col()


class AIMessage(Base):
    __tablename__ = "ai_messages"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(nullable=False)

    created_at: Mapped[datetime] = created_at_col()
