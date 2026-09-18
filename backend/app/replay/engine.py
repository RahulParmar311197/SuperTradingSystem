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


# Every number this engine produces is journalled by
# `app/replay/persistence.py` into `Numeric(18, 6)` columns --
# `replay_orders.pnl` and `replay_sessions.balance` -- which hold at most
# 999999999999.999999.
#
# `ReplayOrderRequest` bounds the numbers a client *supplies*, and that is
# not enough, because P&L is a product of two of them. Measured after
# those field bounds were in place: a 1e11-unit position (accepted: 1e11
# fits the `quantity` column) closed 100 points from its entry is a P&L of
# 1e13, which reached Postgres as NumericValueOutOfRangeError and 500'd
# the request -- with the trade already closed in this engine's memory and
# nothing written for it, the same engine/journal divergence
# `PlaceOrderRequest`'s bounds were added to close on the live path.
#
# The guard is here rather than at the route because this is where the
# product is formed, and because a stop can fire from `advance()`, which
# has no request to reject. `set_stop`/`set_target` therefore refuse a
# level they could not be filled at, so by the time `advance()` runs the
# arithmetic is already known to fit.
_MAX_JOURNALLED = 1e12


@dataclass(slots=True)
class ReplayTrade:
    direction: Direction
    entry_price: float
    quantity: float
    opened_index: int
    opened_at: datetime
    stop: float | None = None
    target: float | None = None
    # The stop as first placed, kept separate from `stop` because blueprint
    # §43 makes MOVE SL a first-class action and §44 asks for "Average R".
    # R is reward measured in units of the risk actually taken, which is
    # fixed the moment the stop is first placed; `stop` is wherever the
    # user has since moved it. Set once by `set_stop` and never overwritten.
    initial_stop: float | None = None
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

    def _pnl_at(self, exit_price: float) -> float:
        """The P&L this open trade would book at `exit_price`. One
        formula, used by `close` and by the two guards below, so a level
        can never be accepted at a price the close would then reject."""
        trade = self.open_trade
        sign = 1 if trade.direction == Direction.LONG else -1
        return (exit_price - trade.entry_price) * trade.quantity * sign

    def _refuse_a_price_the_journal_cannot_hold(self, exit_price: float) -> None:
        pnl = self._pnl_at(exit_price)
        if abs(pnl) >= _MAX_JOURNALLED or abs(self.balance + pnl) >= _MAX_JOURNALLED:
            raise ReplayError(
                f"A fill at {exit_price} on {self.open_trade.quantity} units entered at "
                f"{self.open_trade.entry_price} would book a P&L of {pnl:.2f}, which this "
                "session cannot record. Use a smaller position or a price nearer the entry."
            )

    def set_stop(self, price: float) -> None:
        if self.open_trade is None:
            raise ReplayError("No open position")
        self._refuse_a_price_the_journal_cannot_hold(price)
        self.open_trade.stop = price
        if self.open_trade.initial_stop is None:
            self.open_trade.initial_stop = price

    def set_target(self, price: float) -> None:
        if self.open_trade is None:
            raise ReplayError("No open position")
        self._refuse_a_price_the_journal_cannot_hold(price)
        self.open_trade.target = price

    move_stop = set_stop
    move_target = set_target

    def close(self, price: float | None = None) -> ReplayTrade:
        if self.open_trade is None:
            raise ReplayError("No open position")
        candle = self.clock.current_candle
        exit_price = price if price is not None else candle.close
        # Before the first mutation below, never after: a close that
        # raised halfway would leave this engine holding a trade the
        # journal has no row for.
        self._refuse_a_price_the_journal_cannot_hold(exit_price)
        trade = self.open_trade
        sign = 1 if trade.direction == Direction.LONG else -1
        trade.exit_price = exit_price
        trade.closed_index = self.clock.cursor
        trade.closed_at = candle.timestamp
        trade.pnl = self._pnl_at(exit_price)
        # Against the stop as first placed, not wherever it has since been
        # moved to. Measuring against the current stop makes R mean
        # something different for every trade and silently drops the most
        # common management action of all: moving to breakeven leaves
        # `stop == entry_price`, so the guard below skipped the trade
        # entirely and a managed winner contributed nothing to
        # `ReplayStatistics.average_r`. A stop trailed to 120 on a 100
        # entry with an initial stop at 90 reported R=1.0 for an exit that
        # really made 2R.
        if trade.initial_stop is not None and trade.entry_price != trade.initial_stop:
            risk_per_unit = abs(trade.entry_price - trade.initial_stop)
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
