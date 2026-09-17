"""Backtest engine (blueprint §46-48, §127).

Reuses the exact same SMC/ICT/Strategy-DSL implementation as replay and
paper trading (`app.smc`, `app.ict`, `app.strategy`) — the blueprint's
explicit requirement that the backtester not run a second, divergent
implementation of the trading logic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.backtest.cost_model import CostModel
from app.database.models.strategy import Direction
from app.ict.engine import ICTConfig, ICTEngine
from app.risk.engine import calculate_position_size
from app.smc.engine import SMCConfig, SMCEngine
from app.smc.types import Candle
from app.strategy.context import EvaluationContext
from app.strategy.dsl import StrategyDefinition
from app.strategy.engine import StrategyEngine

logger = logging.getLogger("backtest.engine")


@dataclass(slots=True)
class OpenBacktestPosition:
    """A position the strategy opened and the data ran out before closing.

    The backtest used to drop this on the floor: `run` kept `open_trade`
    local and returned only completed trades, so a run that opened a
    position and held it to the last candle came back as `[]` --
    indistinguishable from a strategy that never fired at all, and
    `compute_metrics` then reported `total_trades: 0`.

    The bias runs one way. A strategy that holds losers -- a stop wide
    enough that the data window ends before it is touched -- has exactly
    those trades omitted, while its winners closed and counted. That is
    the direction that flatters, and §37's out-of-sample comparison reads
    those same numbers.

    `unrealized_pnl` is marked at the final candle's close and is gross:
    no exit cost is charged, because no exit happened. It is deliberately
    *not* folded into the metrics -- whether an open position should be
    marked to market and counted as a trade is a decision about what a
    backtest reports, not something to change quietly underneath one.
    """

    direction: str
    entry_price: float
    stop: float
    target: float
    quantity: float
    opened_at: datetime
    last_price: float
    unrealized_pnl: float


@dataclass(slots=True)
class BacktestTradeRecord:
    direction: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    r_multiple: float | None
    opened_at: datetime
    closed_at: datetime


class BacktestEngine:
    def __init__(
        self,
        strategy: StrategyDefinition,
        starting_capital: float = 100_000.0,
        cost_model: CostModel | None = None,
        smc_config: SMCConfig | None = None,
        ict_config: ICTConfig | None = None,
        max_position_size: float | None = None,
    ) -> None:
        self.strategy = strategy
        self.starting_capital = starting_capital
        self.cost_model = cost_model or CostModel()
        self.smc_engine = SMCEngine(smc_config)
        self.ict_engine = ICTEngine(ict_config)
        self.strategy_engine = StrategyEngine()
        self.max_position_size = max_position_size
        # Mirrors `ReplayEngine.open_trade`, which has always exposed the
        # position it is holding -- `app/api/replay.py` and
        # `app/replay/persistence.py` both read it. The backtester kept
        # the same state private and discarded it.
        self.open_trade: OpenBacktestPosition | None = None

    def run(self, candles: list[Candle], symbol: str) -> list[BacktestTradeRecord]:
        self.open_trade = None
        trades: list[BacktestTradeRecord] = []
        equity = self.starting_capital
        open_trade: dict | None = None

        for i in range(len(candles)):
            visible = candles[: i + 1]
            candle = candles[i]

            if open_trade is not None:
                closed = self._check_exit(open_trade, candle)
                if closed is not None:
                    trades.append(closed)
                    equity += closed.pnl
                    open_trade = None
                    continue

            if open_trade is None and len(visible) >= 3:
                smc_context = self.smc_engine.analyze(visible)
                ict_context = self.ict_engine.analyze(visible)
                context = EvaluationContext(
                    symbol=symbol,
                    timeframe=self.strategy.timeframe,
                    timestamp=candle.timestamp,
                    current_price=candle.close,
                    smc=smc_context,
                    ict=ict_context,
                    current_index=len(visible) - 1,
                )
                result = self.strategy_engine.evaluate(self.strategy, context)
                # A "retest" entry (fvg_retest/order_block_retest) names a
                # price level inside a zone that formed in the past -- it
                # matches the instant an unmitigated FVG/order block
                # exists, not once price has actually traded there again
                # (see app/strategy/engine.py's _resolve_entry_and_stop).
                # Filling unconditionally at that level -- as this used to
                # do -- opened a phantom position at a price the simulated
                # market may never have offered on this candle at all,
                # systematically distorting backtest P&L for every
                # retest-style strategy. A real retest/limit order only
                # fills once price actually trades through its level, so
                # only open here when this candle's own [low, high] range
                # contains `entry` -- for a market-entry strategy (entry ==
                # candle.close) that's always true, so this changes nothing
                # for that entry type.
                if result.matched and candle.low <= result.entry <= candle.high:
                    open_trade = self._open_trade(result, candle, equity)

        if open_trade is not None and candles:
            self.open_trade = self._describe_open(open_trade, candles[-1])
            logger.warning(
                "Backtest ended with an open %s position in %s opened at %s (entry %.4f, stop %.4f): "
                "it is reported as `open_trade` and is NOT counted in the returned trades or in any "
                "metric computed from them.",
                self.open_trade.direction, symbol, self.open_trade.opened_at,
                self.open_trade.entry_price, self.open_trade.stop,
            )

        return trades

    @staticmethod
    def _describe_open(open_trade: dict, last: Candle) -> OpenBacktestPosition:
        sign = 1 if open_trade["direction"] == Direction.LONG else -1
        return OpenBacktestPosition(
            direction=open_trade["direction"].value,
            entry_price=open_trade["entry_price"],
            stop=open_trade["stop"],
            target=open_trade["target"],
            quantity=open_trade["quantity"],
            opened_at=open_trade["opened_at"],
            last_price=last.close,
            # Gross, and marked at the close: no exit happened, so no exit
            # cost is charged and no fill price is being claimed.
            unrealized_pnl=(last.close - open_trade["entry_price"]) * open_trade["quantity"] * sign,
        )

    def _open_trade(self, result, candle: Candle, equity: float) -> dict:
        is_long = result.direction.lower() == "bullish"
        quantity = calculate_position_size(
            equity, self.strategy.risk.risk_percent, result.entry, result.stop, self.max_position_size
        )
        entry_price = self.cost_model.entry_price(result.entry, is_long)
        return {
            "direction": Direction.LONG if is_long else Direction.SHORT,
            "entry_price": entry_price,
            "stop": result.stop,
            "target": result.target,
            "quantity": quantity,
            "opened_at": candle.timestamp,
        }

    def _check_exit(self, open_trade: dict, candle: Candle) -> BacktestTradeRecord | None:
        is_long = open_trade["direction"] == Direction.LONG
        stop, target = open_trade["stop"], open_trade["target"]
        hit_price = None

        if is_long:
            if candle.low <= stop:
                hit_price = stop
            elif candle.high >= target:
                hit_price = target
        else:
            if candle.high >= stop:
                hit_price = stop
            elif candle.low <= target:
                hit_price = target

        if hit_price is None:
            return None

        exit_price = self.cost_model.exit_price(hit_price, is_long)
        quantity = open_trade["quantity"]
        sign = 1 if is_long else -1
        gross_pnl = (exit_price - open_trade["entry_price"]) * quantity * sign
        costs = self.cost_model.round_trip_costs(
            open_trade["entry_price"] * quantity, exit_price * quantity
        )
        pnl = gross_pnl - costs

        risk_per_unit = abs(open_trade["entry_price"] - stop)
        r_multiple = ((exit_price - open_trade["entry_price"]) * sign) / risk_per_unit if risk_per_unit else None

        return BacktestTradeRecord(
            direction=open_trade["direction"].value,
            entry_price=open_trade["entry_price"],
            exit_price=exit_price,
            quantity=quantity,
            pnl=pnl,
            r_multiple=r_multiple,
            opened_at=open_trade["opened_at"],
            closed_at=candle.timestamp,
        )
