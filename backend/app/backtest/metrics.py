"""Backtest performance report (blueprint §48)."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass(slots=True)
class BacktestMetricsResult:
    total_return: float
    net_profit: float
    win_rate: float
    profit_factor: float | None
    expectancy: float | None
    max_drawdown: float
    sharpe: float | None
    sortino: float | None
    average_win: float | None
    average_loss: float | None
    average_r: float | None
    total_trades: int
    long_trades: int
    short_trades: int
    equity_curve: list[float] = field(default_factory=list)
    drawdown_curve: list[float] = field(default_factory=list)
    monthly_returns: dict[str, float] = field(default_factory=dict)


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)


def _downside_deviation(values: list[float], target: float = 0.0) -> float:
    """Root-mean-square shortfall below `target`, over **every** value.

    This is the Sortino ratio's denominator, and it is not a standard
    deviation of the losses. The distinction is the whole bug this
    replaced: the old code took `_std` of only the negative returns, i.e.
    how much the losses differed *from each other*, which answers a
    different question and gives a different number. Measured on a
    100,000 account:

    * three wins and three identical -2,000 losses -> the old form
      reported `None` ("undefined", because identical losses have zero
      spread) where the ratio is 1.0607. That case is not exotic here:
      `RiskLimits.risk_per_trade_pct` sizes every position so that a stop
      costs the same fixed fraction, so equal-sized losses are the
      *normal* shape for this platform's own strategies.
    * a single losing trade -> 1.0 against a true 2.0, from a third
      definition (`abs(r)`) the old code switched to below two losses.
    * losses of differing size -> 0.8729 against a true 0.7127, i.e.
      flattering by 22%.

    Divided by N, every observation, not by the count of losing ones: a
    winning period genuinely contributes zero downside, and dividing by
    only the losers would make a strategy look better the more often it
    won, which inverts the metric. That also keeps it comparable with
    `sharpe` above, which divides by the same N. The N vs N-1 difference
    from `_std` is deliberate too -- this is an RMS about a fixed target,
    not a dispersion estimated about a sample mean.
    """
    if not values:
        return 0.0
    return math.sqrt(sum(min(v - target, 0.0) ** 2 for v in values) / len(values))


def compute_metrics(trades: list, starting_capital: float) -> BacktestMetricsResult:
    """`trades` is a list of objects with: direction ("LONG"/"SHORT"), pnl,
    r_multiple (optional), closed_at (datetime)."""
    pnls = [t.pnl for t in trades]
    net_profit = sum(pnls)
    total_return = net_profit / starting_capital * 100 if starting_capital else 0.0

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    win_rate = len(wins) / len(trades) if trades else 0.0

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    average_win = gross_profit / len(wins) if wins else None
    average_loss = -gross_loss / len(losses) if losses else None
    expectancy = (net_profit / len(trades)) if trades else None

    r_multiples = [t.r_multiple for t in trades if getattr(t, "r_multiple", None) is not None]
    average_r = sum(r_multiples) / len(r_multiples) if r_multiples else None

    equity_curve: list[float] = [starting_capital]
    drawdown_curve: list[float] = [0.0]
    equity = starting_capital
    peak = starting_capital
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        equity_curve.append(equity)
        drawdown_curve.append(peak - equity)
    max_drawdown = max(drawdown_curve) if drawdown_curve else 0.0

    returns_pct = [p / starting_capital for p in pnls] if starting_capital else []
    sharpe = None
    sortino = None
    if returns_pct:
        mean_return = sum(returns_pct) / len(returns_pct)
        std_return = _std(returns_pct)
        sharpe = mean_return / std_return if std_return > 0 else None

        # `None`, never `float("inf")`, when nothing fell below the
        # target -- `backtest_metrics.sortino` is `Numeric(10, 4)` and the
        # replay statistics alongside it go into a Postgres `json` column,
        # neither of which can hold an infinity (see
        # app/replay/statistics.py's `profit_factor` for the session that
        # wedged on exactly that).
        downside_deviation = _downside_deviation(returns_pct)
        sortino = mean_return / downside_deviation if downside_deviation > 0 else None

    monthly_returns: dict[str, float] = defaultdict(float)
    for trade in trades:
        closed_at = getattr(trade, "closed_at", None)
        if closed_at is not None:
            key = f"{closed_at.year:04d}-{closed_at.month:02d}"
            monthly_returns[key] += trade.pnl

    return BacktestMetricsResult(
        total_return=round(total_return, 4),
        net_profit=round(net_profit, 4),
        win_rate=round(win_rate, 4),
        profit_factor=round(profit_factor, 4) if profit_factor is not None else None,
        expectancy=round(expectancy, 4) if expectancy is not None else None,
        max_drawdown=round(max_drawdown, 4),
        sharpe=round(sharpe, 4) if sharpe is not None else None,
        sortino=round(sortino, 4) if sortino is not None else None,
        average_win=round(average_win, 4) if average_win is not None else None,
        average_loss=round(average_loss, 4) if average_loss is not None else None,
        average_r=round(average_r, 4) if average_r is not None else None,
        total_trades=len(trades),
        long_trades=sum(1 for t in trades if t.direction == "LONG"),
        short_trades=sum(1 for t in trades if t.direction == "SHORT"),
        equity_curve=equity_curve,
        drawdown_curve=drawdown_curve,
        monthly_returns=dict(monthly_returns),
    )
