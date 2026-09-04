import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, JSON, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.models.common import created_at_col, updated_at_col, uuid_pk
from app.database.session import Base


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"
    PENDING_VERIFICATION = "PENDING_VERIFICATION"


class UserRole(StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class TradingPermission(StrEnum):
    VIEW = "VIEW"
    ANALYZE = "ANALYZE"
    PAPER_TRADE = "PAPER_TRADE"
    LIVE_TRADE = "LIVE_TRADE"
    AUTO_TRADE = "AUTO_TRADE"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = uuid_pk()
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="user_role"), default=UserRole.USER, nullable=False
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status"), default=UserStatus.ACTIVE, nullable=False
    )
    trading_permissions: Mapped[list[str]] = mapped_column(
        JSON, default=list
    )  # list[TradingPermission.value]

    # Autonomous trading (blueprint §89, §102): OFF by default, and never
    # flipped on by anything but an explicit user action — see
    # app/api/auto_trading.py's `confirm: true` requirement.
    auto_trading_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_trading_risk_per_trade_pct: Mapped[float] = mapped_column(Numeric(6, 3), default=0.5, nullable=False)
    auto_trading_daily_loss_limit_pct: Mapped[float] = mapped_column(Numeric(6, 3), default=2.0, nullable=False)
    auto_trading_max_trades_per_day: Mapped[int] = mapped_column(Integer, default=10, nullable=False)
    auto_trading_max_positions: Mapped[int] = mapped_column(Integer, default=5, nullable=False)

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    sessions: Mapped[list["UserSession"]] = relationship(back_populates="user")
    broker_accounts: Mapped[list["BrokerAccount"]] = relationship(back_populates="user")


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    device_info: Mapped[str | None] = mapped_column(String(500))
    revoked: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()

    user: Mapped["User"] = relationship(back_populates="sessions")


class BrokerName(StrEnum):
    DHAN = "DHAN"
    UPSTOX = "UPSTOX"
    PAPER = "PAPER"


class BrokerAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISCONNECTED = "DISCONNECTED"
    ERROR = "ERROR"


class BrokerAccount(Base):
    __tablename__ = "broker_accounts"

    id: Mapped[uuid.UUID] = uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    broker: Mapped[BrokerName] = mapped_column(Enum(BrokerName, name="broker_name"), nullable=False)
    encrypted_credentials: Mapped[str] = mapped_column(nullable=False)
    status: Mapped[BrokerAccountStatus] = mapped_column(
        Enum(BrokerAccountStatus, name="broker_account_status"),
        default=BrokerAccountStatus.ACTIVE,
        nullable=False,
    )

    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    user: Mapped["User"] = relationship(back_populates="broker_accounts")
