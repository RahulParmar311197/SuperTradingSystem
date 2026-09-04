import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import created_at_col, uuid_pk
from app.database.session import Base


class OptionChainSnapshot(Base):
    __tablename__ = "option_chains"

    id: Mapped[uuid.UUID] = uuid_pk()
    underlying: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expiry: Mapped[date] = mapped_column(Date, nullable=False)
    spot_price: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()


class OptionContract(Base):
    __tablename__ = "option_contracts"

    id: Mapped[uuid.UUID] = uuid_pk()
    instrument_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("instruments.id"), nullable=False, index=True
    )
    chain_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("option_chains.id"), nullable=False, index=True
    )
    strike: Mapped[float] = mapped_column(Numeric(18, 4), nullable=False)
    option_type: Mapped[str] = mapped_column(String(4), nullable=False)

    created_at: Mapped[datetime] = created_at_col()


class OptionSnapshot(Base):
    __tablename__ = "option_snapshots"

    id: Mapped[uuid.UUID] = uuid_pk()
    option_contract_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("option_contracts.id"), nullable=False, index=True
    )
    bid: Mapped[float | None] = mapped_column(Numeric(18, 6))
    ask: Mapped[float | None] = mapped_column(Numeric(18, 6))
    ltp: Mapped[float | None] = mapped_column(Numeric(18, 6))
    volume: Mapped[float] = mapped_column(Numeric(24, 6), default=0)
    open_interest: Mapped[float] = mapped_column(Numeric(24, 6), default=0)
    iv: Mapped[float | None] = mapped_column(Numeric(10, 6))
    delta: Mapped[float | None] = mapped_column(Numeric(10, 6))
    gamma: Mapped[float | None] = mapped_column(Numeric(10, 6))
    theta: Mapped[float | None] = mapped_column(Numeric(10, 6))
    vega: Mapped[float | None] = mapped_column(Numeric(10, 6))
    snapshot_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    created_at: Mapped[datetime] = created_at_col()
