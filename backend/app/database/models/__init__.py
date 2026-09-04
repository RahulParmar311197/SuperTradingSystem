"""All ORM models, imported here so Base.metadata sees every table
(required for Alembic autogenerate)."""

from app.database.models.ai import AIDecision, AIDecisionType, AIMessage
from app.database.models.backtest import Backtest, BacktestMetrics, BacktestStatus, BacktestTrade
from app.database.models.instruments import Instrument, MarketType, OptionType
from app.database.models.market import Candle, Tick
from app.database.models.notifications import Notification, NotificationType
from app.database.models.options import OptionChainSnapshot, OptionContract, OptionSnapshot
from app.database.models.replay import ReplayOrder, ReplaySession, ReplayStatus
from app.database.models.risk import AuditLog, RiskDecision, RiskEvent
from app.database.models.strategy import Direction, Setup, Signal, Strategy, StrategyVersion
from app.database.models.trading import (
    ExecutionMode,
    Order,
    OrderEvent,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    Position,
    Trade,
)
from app.database.models.users import (
    BrokerAccount,
    BrokerAccountStatus,
    BrokerName,
    TradingPermission,
    User,
    UserRole,
    UserSession,
    UserStatus,
)

__all__ = [
    "AIDecision",
    "AIDecisionType",
    "AIMessage",
    "AuditLog",
    "Backtest",
    "BacktestMetrics",
    "BacktestStatus",
    "BacktestTrade",
    "BrokerAccount",
    "BrokerAccountStatus",
    "BrokerName",
    "Candle",
    "Direction",
    "ExecutionMode",
    "Instrument",
    "MarketType",
    "Notification",
    "NotificationType",
    "OptionChainSnapshot",
    "OptionContract",
    "OptionSnapshot",
    "OptionType",
    "Order",
    "OrderEvent",
    "OrderStatus",
    "OrderType",
    "PortfolioSnapshot",
    "Position",
    "ReplayOrder",
    "ReplaySession",
    "ReplayStatus",
    "RiskDecision",
    "RiskEvent",
    "Setup",
    "Signal",
    "Strategy",
    "StrategyVersion",
    "Tick",
    "Trade",
    "TradingPermission",
    "User",
    "UserRole",
    "UserSession",
    "UserStatus",
]
