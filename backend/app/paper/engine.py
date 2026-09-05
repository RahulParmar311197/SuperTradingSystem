"""Paper trading engine (blueprint §49): Strategy -> Risk -> Paper
Execution -> Position Manager -> Portfolio, using the exact same SMC/ICT
strategy evaluation as backtest and replay, plus the full order/broker
stack from `app.trading` and `app.brokers` (fees, slippage, partial fills,
rejections are simulated by the underlying `MockBroker`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.brokers.mock import MockBroker
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.ict.engine import ICTConfig, ICTEngine
from app.risk.engine import RiskEngine, TradeRiskProposal, calculate_position_size
from app.risk.limits import RiskLimits
from app.smc.engine import SMCConfig, SMCEngine
from app.smc.types import Candle
from app.strategy.context import EvaluationContext
from app.strategy.dsl import StrategyDefinition
from app.strategy.engine import StrategyEngine, StrategyEvaluationResult
from app.trading.execution import ExecutionEngine
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager


@dataclass(slots=True)
class PaperTradeOutcome:
    signal: StrategyEvaluationResult | None
    order_created: bool = False
    risk_rejected_reason: str | None = None
    # Which `RiskCheck.name` actually failed (e.g. "daily_loss_limit",
    # "max_open_positions") -- `risk_rejected_reason` above is only the
    # first failed check's free-text `detail`/`name` collapsed into one
    # string by `RiskDecisionResult.reason`, not something a caller can
    # safely pattern-match on. This is the same identity `RiskEngine`
    # already computed (`RiskDecisionResult.failed_checks[0].name`); kept
    # here as a stable string so callers can fire
    # NotificationType.DAILY_LOSS_LIMIT (blueprint §63) instead of a
    # generic ORDER_REJECTED when that specific check is why.
    risk_failed_check: str | None = None
    closed_position_pnl: float | None = None
    # Which side of the bracket actually closed the position -- `None` iff
    # `closed_position_pnl` is also `None` (nothing closed this candle).
    # `_maybe_exit` already knows this (it branches on it explicitly to
    # pick `exit_price`); this just stops that answer from being thrown
    # away, so callers can fire NotificationType.SL_HIT/TP_HIT (blueprint
    # §63) instead of a generic POSITION_CLOSED for every exit.
    exit_reason: Literal["stop_loss", "take_profit"] | None = None


class PaperTradingEngine:
    def __init__(
        self,
        strategy: StrategyDefinition,
        symbol: str,
        account_id: str = "paper-account",
        starting_balance: float = 100_000.0,
        risk_limits: RiskLimits | None = None,
        smc_config: SMCConfig | None = None,
        ict_config: ICTConfig | None = None,
        broker: MockBroker | None = None,
    ) -> None:
        self.strategy = strategy
        self.symbol = symbol
        self.account_id = account_id
        self.candles: list[Candle] = []

        self.smc_engine = SMCEngine(smc_config)
        self.ict_engine = ICTEngine(ict_config)
        self.strategy_engine = StrategyEngine()
        self.risk_engine = RiskEngine(limits=risk_limits or RiskLimits())

        self.broker = broker or MockBroker(starting_balance=starting_balance)
        self.order_manager = OrderManager()
        self.position_manager = PositionManager()
        self.execution_engine = ExecutionEngine(self.broker, self.order_manager, self.position_manager)

        self.trades_today = 0
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        # Blueprint §57 "Repeated order rejection" -- consecutive
        # broker-level rejections (see the update after each fresh order
        # attempt in `on_candle` below).
        self.repeated_rejections = 0
        # Set on first `_roll_risk_window` call, not here -- `None` means
        # "no window established yet", so the first candle never wrongly
        # resets a freshly constructed engine.
        self._risk_day = None
        self._risk_week = None

    def _roll_risk_window(self, now: datetime) -> None:
        """Resets `trades_today`/`daily_pnl` at a day boundary and
        `weekly_pnl` at an (ISO) week boundary, keyed off `now` (the
        current candle's timestamp -- this engine's logical clock, the
        same convention `app.smc.liquidity.detect_session_levels` uses for
        day/week bucketing). Without this, these counters only ever reset
        when the worker process restarts, making RiskEngine.evaluate's
        `max_trades_per_day`/`daily_loss_limit`/`weekly_loss_limit` checks
        lifetime-of-process limits rather than the rolling daily/weekly
        limits they're meant to be -- see docs/ARCHITECTURE.md. Mirrors
        `_UserTradingStack._roll_risk_window` (app/api/orders.py), which
        does the same thing keyed off wall clock instead."""
        today = now.date()
        this_week = now.isocalendar()[:2]
        if self._risk_day is not None and today != self._risk_day:
            self.trades_today = 0
            self.daily_pnl = 0.0
        if self._risk_week is not None and this_week != self._risk_week:
            self.weekly_pnl = 0.0
        self._risk_day = today
        self._risk_week = this_week

    async def on_candle(self, candle: Candle) -> PaperTradeOutcome:
        self._roll_risk_window(candle.timestamp)
        self.candles.append(candle)
        self.broker.set_quote(self.symbol, ltp=candle.close)
        self.position_manager.mark_to_market(self.account_id, self.symbol, candle.close)

        position = self.position_manager.get(self.account_id, self.symbol)
        if position is not None and position.is_open:
            closed_pnl, exit_reason = await self._maybe_exit(position, candle)
            return PaperTradeOutcome(signal=None, closed_position_pnl=closed_pnl, exit_reason=exit_reason)

        if len(self.candles) < 3:
            return PaperTradeOutcome(signal=None)

        context = EvaluationContext(
            symbol=self.symbol,
            timeframe=self.strategy.timeframe,
            timestamp=candle.timestamp,
            current_price=candle.close,
            smc=self.smc_engine.analyze(self.candles),
            ict=self.ict_engine.analyze(self.candles),
        )
        result = self.strategy_engine.evaluate(self.strategy, context)
        if not result.matched:
            return PaperTradeOutcome(signal=result)

        account = await self.broker.get_account()
        proposal = TradeRiskProposal(
            account_id=self.account_id,
            strategy_id=self.strategy.name,
            entry=result.entry,
            stop=result.stop,
            account_balance=account.balance,
            open_positions=len(self.position_manager.open_positions(self.account_id)),
            trades_today=self.trades_today,
            daily_pnl=self.daily_pnl,
            weekly_pnl=self.weekly_pnl,
            current_exposure=0.0,
            strategy_allocation=0.0,
            market_data_age_seconds=0.0,
            broker_healthy=await self.broker.is_healthy(),
            repeated_rejections=self.repeated_rejections,
        )
        decision = self.risk_engine.evaluate(proposal)
        if not decision.approved:
            failed_check = decision.failed_checks[0].name if decision.failed_checks else None
            return PaperTradeOutcome(signal=result, risk_rejected_reason=decision.reason, risk_failed_check=failed_check)

        quantity = calculate_position_size(
            account.balance, self.strategy.risk.risk_percent, result.entry, result.stop, self.risk_engine.limits.max_position_size
        )
        direction = Direction.LONG if result.direction.lower() == "bullish" else Direction.SHORT
        idempotency_key = f"{self.account_id}:{self.strategy.name}:{candle.timestamp.isoformat()}"

        order, created = self.order_manager.create_order(
            idempotency_key, self.account_id, self.symbol, direction, OrderType.MARKET, quantity
        )
        if created:
            self.order_manager.transition(order.id, OrderStatus.VALIDATING)
            self.order_manager.transition(order.id, OrderStatus.RISK_APPROVED)
            await self.execution_engine.submit(order.id)
            self.trades_today += 1
            final_order = self.order_manager.get(order.id)
            # See `_UserTradingStack`'s identical update in
            # app/api/orders.py's `place_order` for why.
            self.repeated_rejections = self.repeated_rejections + 1 if final_order.status == OrderStatus.REJECTED else 0
            new_position = self.position_manager.get(self.account_id, self.symbol)
            if new_position is not None:
                new_position.stop = result.stop
                new_position.target = result.target

        return PaperTradeOutcome(signal=result, order_created=created)

    async def _maybe_exit(self, position, candle: Candle) -> tuple[float | None, Literal["stop_loss", "take_profit"] | None]:
        is_long = position.is_long
        exit_price = None
        exit_reason: Literal["stop_loss", "take_profit"] | None = None
        if is_long:
            if position.stop is not None and candle.low <= position.stop:
                exit_price = position.stop
                exit_reason = "stop_loss"
            elif position.target is not None and candle.high >= position.target:
                exit_price = position.target
                exit_reason = "take_profit"
        else:
            if position.stop is not None and candle.high >= position.stop:
                exit_price = position.stop
                exit_reason = "stop_loss"
            elif position.target is not None and candle.low <= position.target:
                exit_price = position.target
                exit_reason = "take_profit"

        if exit_price is None:
            return None, None

        realized_before = position.realized_pnl
        self.broker.set_quote(self.symbol, ltp=exit_price)
        closing_direction = Direction.SHORT if is_long else Direction.LONG
        idempotency_key = f"{self.account_id}:{self.symbol}:close:{candle.timestamp.isoformat()}"
        order, created = self.order_manager.create_order(
            idempotency_key, self.account_id, self.symbol, closing_direction, OrderType.MARKET, abs(position.quantity)
        )
        if created:
            self.order_manager.transition(order.id, OrderStatus.VALIDATING)
            self.order_manager.transition(order.id, OrderStatus.RISK_APPROVED)
            await self.execution_engine.submit(order.id)

        pnl = position.realized_pnl - realized_before
        self.daily_pnl += pnl
        self.weekly_pnl += pnl
        return pnl, exit_reason
