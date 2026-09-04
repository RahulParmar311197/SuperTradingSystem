import uuid
from datetime import date
from enum import StrEnum

from sqlalchemy import Boolean, Date, Enum, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.models.common import uuid_pk
from app.database.session import Base


class MarketType(StrEnum):
    EQUITY = "EQUITY"
    FUTURES = "FUTURES"
    OPTIONS = "OPTIONS"
    FOREX = "FOREX"
    CRYPTO = "CRYPTO"
    COMMODITY = "COMMODITY"


class OptionType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


class Instrument(Base):
    __tablename__ = "instruments"

    id: Mapped[uuid.UUID] = uuid_pk()
    symbol: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    market: Mapped[MarketType] = mapped_column(Enum(MarketType, name="market_type"), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(32), nullable=False)
    underlying: Mapped[str | None] = mapped_column(String(64))
    expiry: Mapped[date | None] = mapped_column(Date)
    strike: Mapped[float | None] = mapped_column(Numeric(18, 4))
    option_type: Mapped[OptionType | None] = mapped_column(Enum(OptionType, name="option_type"))
    lot_size: Mapped[int] = mapped_column(default=1)
    tick_size: Mapped[float] = mapped_column(Numeric(18, 6), default=0.05)
    currency: Mapped[str] = mapped_column(String(8), default="INR")
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    __table_args__ = ()
