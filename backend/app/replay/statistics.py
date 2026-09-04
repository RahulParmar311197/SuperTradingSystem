"""Replay statistics (blueprint §44)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class ReplayStatistics:
    starting_balance: float
    ending_balance: float
    net_pnl: float
    win_rate: float
    profit_factor: float | None
    max_drawdown: float
    trades: int
    average_r: float | None
    best_trade: float | None
    worst_trade: float | None


def compute_statistics(closed_trade_pnls: list[float], starting_balance: float, r_multiples: list[float] | None = None) -> ReplayStatistics:
    trades = len(closed_trade_pnls)
    net_pnl = sum(closed_trade_pnls)
    ending_balance = starting_balance + net_pnl

    wins = [p for p in closed_trade_pnls if p > 0]
    losses = [p for p in closed_trade_pnls if p < 0]
    win_rate = len(wins) / trades if trades else 0.0

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (None if gross_profit == 0 else float("inf"))

    equity = starting_balance
    peak = starting_balance
    max_drawdown = 0.0
    for pnl in closed_trade_pnls:
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    r_multiples = r_multiples or []
    average_r = sum(r_multiples) / len(r_multiples) if r_multiples else None

    return ReplayStatistics(
        starting_balance=starting_balance,
        ending_balance=ending_balance,
        net_pnl=net_pnl,
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 4) if isinstance(profit_factor, float) and profit_factor != float("inf") else profit_factor,
        max_drawdown=round(max_drawdown, 4),
        trades=trades,
        average_r=round(average_r, 4) if average_r is not None else None,
        best_trade=max(closed_trade_pnls) if closed_trade_pnls else None,
        worst_trade=min(closed_trade_pnls) if closed_trade_pnls else None,
    )
