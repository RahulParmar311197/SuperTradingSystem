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
from app.risk.kill_switch import load_kill_switch_state
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
    # The full per-check pass/fail map from the `RiskDecisionResult` that
    # was actually evaluated this candle -- `None` iff no risk decision was
    # made at all (no signal matched, or a position was already open).
    # Set on *both* the approved and rejected paths so callers (app/api/paper.py,
    # app/workers/auto_trade_worker.py) can write a `RiskEvent` audit row for
    # every decision, mirroring what app/api/orders.py and app/api/options.py
    # already do -- before this field existed, paper trading and autonomous
    # trading evaluated the exact same `RiskEngine` but the decision (approve
    # *or* reject) vanished the instant `on_candle` returned, leaving
    # `GET /admin/risk-events` blind to every paper/auto-trade decision ever
    # made.
    risk_checks: dict[str, bool] | None = None
    closed_position_pnl: float | None = None
    # Which side of the bracket actually closed the position -- `None` iff
    # `closed_position_pnl` is also `None` (nothing closed this candle).
    # `_maybe_exit` already knows this (it branches on it explicitly to
    # pick `exit_price`); this just stops that answer from being thrown
    # away, so callers can fire NotificationType.SL_HIT/TP_HIT (blueprint
    # §63) instead of a generic POSITION_CLOSED for every exit.
    exit_reason: Literal["stop_loss", "take_profit"] | None = None
    # The real fill price behind `closed_position_pnl` -- `None` iff
    # `closed_position_pnl` is also `None`. `_maybe_exit` fills the closing
    # order at the stop/target level that triggered it, not at the
    # candle's `close` -- a stop-loss can trigger intraday and still close
    # green on the candle's close, so `candle.close`/`latest.close` (what
    # callers used before this field existed) was never the price that
    # actually produced `closed_position_pnl`.
    exit_price: float | None = None


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
        position_manager: PositionManager | None = None,
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
        # Callers driving several engines for the same account_id across
        # different symbols (AutoTradeSupervisor, one engine per
        # (strategy, instrument) pair) must pass the *same*
        # PositionManager instance to every one of them -- see the
        # `current_exposure`/`open_positions` comment in `on_candle`
        # below for why a private, per-engine one silently defeats
        # account-wide risk limits.
        self.position_manager = position_manager or PositionManager()
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
            closed_pnl, exit_reason, exit_price = await self._maybe_exit(position, candle)
            return PaperTradeOutcome(signal=None, closed_position_pnl=closed_pnl, exit_reason=exit_reason, exit_price=exit_price)

        if len(self.candles) < 3:
            return PaperTradeOutcome(signal=None)

        context = EvaluationContext(
            symbol=self.symbol,
            timeframe=self.strategy.timeframe,
            timestamp=candle.timestamp,
            current_price=candle.close,
            smc=self.smc_engine.analyze(self.candles),
            ict=self.ict_engine.analyze(self.candles),
            current_index=len(self.candles) - 1,
        )
        result = self.strategy_engine.evaluate(self.strategy, context)
        if not result.matched:
            return PaperTradeOutcome(signal=result)

        account = await self.broker.get_account()
        # `self.position_manager` is shared across every engine driving
        # this same account_id (see AutoTradeSupervisor, which runs one
        # PaperTradingEngine per (strategy, instrument) pair) precisely so
        # that this aggregates *all* of the account's open positions, not
        # just the one for `self.symbol` -- a private, per-engine
        # PositionManager can never see more than one position at a time
        # (this method already returned early above if `self.symbol`
        # itself has one open), which silently made `open_positions` cap
        # at 1 and `current_exposure` a permanent 0.0 for every engine,
        # defeating RiskLimits.max_open_positions/max_exposure_pct as
        # account-wide caps on unattended autonomous trading.
        open_positions = self.position_manager.open_positions(self.account_id)
        current_exposure = sum(abs(p.quantity) * p.average_price for p in open_positions)
        # Notional this specific strategy already has open, across every
        # symbol it trades -- not just `self.symbol` (this method already
        # returned early above if that one has an open position). Only
        # `PaperTradingEngine`'s own post-fill code ever stamps
        # `PositionRecord.strategy_id` (see below), so this is 0.0 exactly
        # when it should be: a strategy with nothing open yet.
        strategy_allocation = sum(
            abs(p.quantity) * p.average_price for p in open_positions if p.strategy_id == self.strategy.name
        )
        proposal = TradeRiskProposal(
            account_id=self.account_id,
            strategy_id=self.strategy.name,
            entry=result.entry,
            stop=result.stop,
            account_balance=account.balance,
            open_positions=len(open_positions),
            trades_today=self.trades_today,
            daily_pnl=self.daily_pnl,
            weekly_pnl=self.weekly_pnl,
            current_exposure=current_exposure,
            strategy_allocation=strategy_allocation,
            market_data_age_seconds=0.0,
            broker_healthy=await self.broker.is_healthy(),
            repeated_rejections=self.repeated_rejections,
            # `len(self.candles) < 3` already returned above, so
            # `self.candles[-2]` is always safe here. No Redis primitive
            # needed like app/api/orders.py's live path -- this engine
            # already has its own candle history to diff the current tick
            # against.
            recent_price_jump_pct=(
                abs(self.candles[-1].close - self.candles[-2].close) / self.candles[-2].close * 100
                if self.candles[-2].close
                else 0.0
            ),
        )
        # Blueprint §58: refreshed on every candle, not cached at
        # construction time, so a kill triggered via the admin endpoint
        # (from this or any other process -- this engine and the
        # AutoTradeSupervisor that drives it in app.workers.auto_trade_worker
        # both reuse this method) takes effect on the very next evaluation.
        # See app.risk.kill_switch.load_kill_switch_state.
        self.risk_engine.kill_switch = await load_kill_switch_state(self.account_id, self.strategy.name)
        decision = self.risk_engine.evaluate(proposal)
        risk_checks = {c.name: c.passed for c in decision.checks}
        if not decision.approved:
            failed_check = decision.failed_checks[0].name if decision.failed_checks else None
            return PaperTradeOutcome(
                signal=result, risk_rejected_reason=decision.reason, risk_failed_check=failed_check, risk_checks=risk_checks
            )

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
                # Blueprint §57 "Maximum strategy allocation" -- attribute
                # this fresh entry to the strategy that opened it, so a
                # later candle's `strategy_allocation` computation above
                # (for this engine or a sibling one sharing the same
                # PositionManager) can actually see it.
                new_position.strategy_id = self.strategy.name

        return PaperTradeOutcome(signal=result, order_created=created, risk_checks=risk_checks)

    async def _maybe_exit(
        self, position, candle: Candle
    ) -> tuple[float | None, Literal["stop_loss", "take_profit"] | None, float | None]:
        is_long = position.is_long
        trigger_price = None
        exit_reason: Literal["stop_loss", "take_profit"] | None = None
        if is_long:
            if position.stop is not None and candle.low <= position.stop:
                trigger_price = position.stop
                exit_reason = "stop_loss"
            elif position.target is not None and candle.high >= position.target:
                trigger_price = position.target
                exit_reason = "take_profit"
        else:
            if position.stop is not None and candle.high >= position.stop:
                trigger_price = position.stop
                exit_reason = "stop_loss"
            elif position.target is not None and candle.low <= position.target:
                trigger_price = position.target
                exit_reason = "take_profit"

        if trigger_price is None:
            return None, None, None

        realized_before = position.realized_pnl
        self.broker.set_quote(self.symbol, ltp=trigger_price)
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
        # The order's own recorded fill, not `trigger_price` -- same
        # reasoning as app/api/orders.py's record_trade fix: `pnl` above
        # was already derived from this fill via PositionManager.apply_fill,
        # so this is the value that actually matches it, not a
        # pre-execution local that happens to equal it only while this
        # engine's MockBroker has zero slippage.
        final_order = self.order_manager.get(order.id)
        return pnl, exit_reason, final_order.average_fill_price
