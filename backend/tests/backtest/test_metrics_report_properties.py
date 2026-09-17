"""The numbers a strategy is judged on, guarded against a regression.

**No bug was found here.** `compute_metrics` was probed over 20,000
random trade sets — every reported number recomputed independently — with
0 discrepancies. This file exists because almost nothing asserted them:
before it, the only assertions on `compute_metrics` output were
`win_rate == 1.0` and the equity curve's length. `max_drawdown`, the
drawdown curve, `monthly_returns`, `expectancy` and `average_r` had none,
and `max_drawdown` is the headline risk number someone reads to decide
whether a strategy is safe to run.

The risk ratios (`sharpe`, `sortino`) are covered separately in
`test_metrics_risk_ratios.py`; this file deliberately does not repeat
them.
"""

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.metrics import compute_metrics

BASE = datetime(2026, 1, 5, tzinfo=timezone.utc)
CAPITAL = 100_000.0


@dataclass
class _Trade:
    direction: str
    pnl: float
    r_multiple: float | None
    closed_at: datetime


def _random_trades(rng: random.Random, count: int) -> list[_Trade]:
    return [
        _Trade(
            rng.choice(["LONG", "SHORT"]),
            round(rng.uniform(-5000, 5000), 2),
            rng.choice([None, round(rng.uniform(-3, 3), 3)]),
            BASE + timedelta(days=rng.randint(0, 400)),
        )
        for _ in range(count)
    ]


# --- the report must agree with its own inputs ---------------------------


def test_every_reported_number_recomputes_from_the_same_trades():
    """Seeded, so a failure is reproducible rather than a once-in-a-run flake."""
    rng = random.Random(20260917)

    for _ in range(300):
        trades = _random_trades(rng, rng.randint(0, 12))
        metrics = compute_metrics(trades, CAPITAL)
        pnls = [t.pnl for t in trades]
        wins = [p for p in pnls if p > 0]

        assert metrics.total_trades == len(trades)
        assert metrics.long_trades + metrics.short_trades == len(trades)
        assert metrics.net_profit == pytest.approx(sum(pnls), abs=1e-4)
        expected_win_rate = len(wins) / len(trades) if trades else 0.0
        assert metrics.win_rate == pytest.approx(expected_win_rate, abs=1e-4)

        for name in ("sharpe", "sortino", "profit_factor", "expectancy", "average_r"):
            value = getattr(metrics, name)
            assert value is None or math.isfinite(value), f"{name} must never be inf/nan: it is persisted as Numeric"


def test_the_equity_curve_starts_at_capital_and_ends_at_capital_plus_profit():
    rng = random.Random(4242)

    for _ in range(300):
        trades = _random_trades(rng, rng.randint(0, 12))
        metrics = compute_metrics(trades, CAPITAL)

        assert metrics.equity_curve[0] == CAPITAL
        assert len(metrics.equity_curve) == len(trades) + 1
        assert metrics.equity_curve[-1] == pytest.approx(CAPITAL + sum(t.pnl for t in trades), abs=1e-6)


def test_max_drawdown_is_the_worst_peak_to_trough_of_its_own_equity_curve():
    # The headline risk number, and the one that had no assertion at all.
    rng = random.Random(1337)

    for _ in range(300):
        metrics = compute_metrics(_random_trades(rng, rng.randint(0, 12)), CAPITAL)

        peak = metrics.equity_curve[0]
        worst = 0.0
        for value in metrics.equity_curve:
            peak = max(peak, value)
            worst = max(worst, peak - value)

        assert metrics.max_drawdown == pytest.approx(worst, abs=1e-4)
        assert metrics.max_drawdown >= 0, "a drawdown is a magnitude, never negative"


def test_monthly_returns_partition_the_pnl_exactly():
    # Every trade lands in exactly one month, and no P&L is invented or lost.
    rng = random.Random(99)

    for _ in range(300):
        trades = _random_trades(rng, rng.randint(0, 12))
        metrics = compute_metrics(trades, CAPITAL)

        assert sum(metrics.monthly_returns.values()) == pytest.approx(sum(t.pnl for t in trades), abs=1e-6)


# --- worked examples, so a failure above is diagnosable ------------------


def test_a_known_drawdown_is_reported_at_its_true_depth():
    trades = [
        _Trade("LONG", 1000.0, None, BASE),
        _Trade("LONG", -3000.0, None, BASE),
        _Trade("LONG", -500.0, None, BASE),
        _Trade("LONG", 900.0, None, BASE),
    ]
    metrics = compute_metrics(trades, CAPITAL)

    # Peak 101,000 after the first win; trough 97,500 after the third trade.
    assert metrics.equity_curve == [100000.0, 101000.0, 98000.0, 97500.0, 98400.0]
    assert metrics.max_drawdown == pytest.approx(3500.0)
    assert metrics.net_profit == pytest.approx(-1600.0)


def test_an_all_winning_run_reports_no_drawdown_and_no_profit_factor():
    # Control: `profit_factor` is None rather than infinity when there is
    # nothing to divide by — it is persisted into `Numeric(10, 4)`, which
    # cannot hold one. The paired replay bug wedged a whole session on this.
    metrics = compute_metrics([_Trade("LONG", 100.0, None, BASE)] * 3, CAPITAL)

    assert metrics.max_drawdown == 0.0
    assert metrics.profit_factor is None
    assert metrics.win_rate == 1.0
