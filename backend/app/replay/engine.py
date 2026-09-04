"""Replay engine (blueprint §41-45, §126): drives a `ReplayClock` forward,
re-runs SMC/ICT analysis on only the visible candles at each step, and lets
a user manually BUY/SELL/SET SL/SET TP/CLOSE/MOVE SL/MOVE TP against
simulated positions — exactly the flow described in §43.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.database.models.strategy import Direction
from app.ict.engine import ICTConfig, ICTContext, ICTEngine
from app.replay.clock import ReplayClock
from app.replay.statistics import ReplayStatistics, compute_statistics
from app.smc.engine import SMCConfig, SMCContext, SMCEngine
from app.smc.types import Candle


class ReplayError(Exception):
    pass


@dataclass(slots=True)
class ReplayTrade:
    direction: Direction
    entry_price: float
    quantity: float
    opened_index: int
    opened_at: datetime
    stop: float | None = None
    target: float | None = None
    exit_price: float | None = None
    closed_index: int | None = None
    closed_at: datetime | None = None
    pnl: float | None = None
    r_multiple: float | None = None
    # Stable identity so app.replay.persistence can upsert the matching
    # `replay_orders` row idempotently across repeated sync calls, the
    # same way app.trading.persistence keys off OrderRecord.id.
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @property
    def is_open(self) -> bool:
        return self.exit_price is None


class ReplayEngine:
    def __init__(
        self,
        candles: list[Candle],
        starting_balance: float = 100_000.0,
        smc_config: SMCConfig | None = None,
        ict_config: ICTConfig | None = None,
    ) -> None:
        self.clock = ReplayClock(candles)
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.smc_engine = SMCEngine(smc_config)
        self.ict_engine = ICTEngine(ict_config)
        self.open_trade: ReplayTrade | None = None
        self.closed_trades: list[ReplayTrade] = []

    def analyze(self) -> tuple[SMCContext, ICTContext]:
        visible = self.clock.visible_candles
        return self.smc_engine.analyze(visible), self.ict_engine.analyze(visible)

    def _open(self, direction: Direction, quantity: float) -> ReplayTrade:
        if self.open_trade is not None:
            raise ReplayError("A position is already open; close it before opening a new one")
        candle = self.clock.current_candle
        trade = ReplayTrade(
            direction=direction,
            entry_price=candle.close,
            quantity=quantity,
            opened_index=self.clock.cursor,
            opened_at=candle.timestamp,
        )
        self.open_trade = trade
        return trade

    def buy(self, quantity: float) -> ReplayTrade:
        return self._open(Direction.LONG, quantity)

    def sell(self, quantity: float) -> ReplayTrade:
        return self._open(Direction.SHORT, quantity)

    def set_stop(self, price: float) -> None:
        if self.open_trade is None:
            raise ReplayError("No open position")
        self.open_trade.stop = price

    def set_target(self, price: float) -> None:
        if self.open_trade is None:
            raise ReplayError("No open position")
        self.open_trade.target = price

    move_stop = set_stop
    move_target = set_target

    def close(self, price: float | None = None) -> ReplayTrade:
        if self.open_trade is None:
            raise ReplayError("No open position")
        candle = self.clock.current_candle
        exit_price = price if price is not None else candle.close
        trade = self.open_trade
        sign = 1 if trade.direction == Direction.LONG else -1
        trade.exit_price = exit_price
        trade.closed_index = self.clock.cursor
        trade.closed_at = candle.timestamp
        trade.pnl = (exit_price - trade.entry_price) * trade.quantity * sign
        if trade.stop is not None and trade.entry_price != trade.stop:
            risk_per_unit = abs(trade.entry_price - trade.stop)
            trade.r_multiple = ((exit_price - trade.entry_price) * sign) / risk_per_unit

        self.balance += trade.pnl
        self.closed_trades.append(trade)
        self.open_trade = None
        return trade

    def _check_stop_target(self, candle: Candle) -> None:
        trade = self.open_trade
        if trade is None:
            return
        if trade.direction == Direction.LONG:
            if trade.stop is not None and candle.low <= trade.stop:
                self.close(price=trade.stop)
            elif trade.target is not None and candle.high >= trade.target:
                self.close(price=trade.target)
        else:
            if trade.stop is not None and candle.high >= trade.stop:
                self.close(price=trade.stop)
            elif trade.target is not None and candle.low <= trade.target:
                self.close(price=trade.target)

    def advance(self, steps: int = 1) -> Candle:
        candle = self.clock.current_candle
        for _ in range(steps):
            if self.clock.is_finished:
                break
            candle = self.clock.next_candle()
            self._check_stop_target(candle)
        return candle

    @property
    def statistics(self) -> ReplayStatistics:
        pnls = [t.pnl for t in self.closed_trades if t.pnl is not None]
        r_multiples = [t.r_multiple for t in self.closed_trades if t.r_multiple is not None]
        return compute_statistics(pnls, self.starting_balance, r_multiples)
