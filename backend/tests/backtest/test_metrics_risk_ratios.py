"""Sharpe and Sortino — the two ratios nothing asserted until now.

`sortino`'s denominator used to be `_std` of the *losing* returns: how
much the losses differed from each other, which is not the downside
deviation the ratio is defined against (the RMS shortfall below the
target, over every observation). Below two losing trades it switched to a
third formula, `abs(r)`.

Measured against the textbook value on a 100,000 account before the fix:

| case                                    | reported | correct |
|-----------------------------------------|----------|---------|
| three wins, three identical -2,000 losses | `None`   | 1.0607  |
| one losing trade                          | 1.0      | 2.0     |
| losses of differing size                  | 0.8729   | 0.7127  |
| three identical losses, no wins           | `None`   | -1.0    |

The first row is the one that matters most here. `RiskLimits.
risk_per_trade_pct` sizes every position so a stop costs the same fixed
fraction of the account, so equal-sized losses are the *normal* shape for
this platform's own strategies — exactly when the spread of losses is
zero and the old form reported the ratio as undefined.
"""

import math
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.backtest.metrics import compute_metrics

CAPITAL = 100_000.0


@dataclass
class _Trade:
    direction: str
    pnl: float
    r_multiple: float | None = None
    closed_at: datetime = datetime(2026, 1, 5, tzinfo=timezone.utc)


def _metrics(pnls: list[float]):
    return compute_metrics([_Trade("LONG", p) for p in pnls], CAPITAL)


def _textbook_sortino(pnls: list[float]) -> float | None:
    """Mean return over downside deviation, written out longhand so this
    is an independent check rather than a copy of the implementation."""
    returns = [p / CAPITAL for p in pnls]
    mean = sum(returns) / len(returns)
    downside = math.sqrt(sum(min(r, 0.0) ** 2 for r in returns) / len(returns))
    return mean / downside if downside > 0 else None


# --- the four measured cases ----------------------------------------------


@pytest.mark.parametrize(
    ("label", "pnls", "expected"),
    [
        ("identical losses", [5000, -2000, 4000, -2000, 6000, -2000], 1.0607),
        ("a single loss", [5000, 4000, -3000, 6000], 2.0),
        ("losses of differing size", [5000, -1000, 4000, -4000, 6000, -2000], 0.7127),
        ("nothing but identical losses", [-2000, -2000, -2000], -1.0),
    ],
)
def test_sortino_matches_the_textbook_ratio(label, pnls, expected):
    assert _metrics(pnls).sortino == pytest.approx(expected, abs=5e-5)
    assert _metrics(pnls).sortino == pytest.approx(_textbook_sortino(pnls), abs=5e-5)


def test_identical_losses_do_not_make_the_ratio_undefined():
    # The regression in its sharpest form: zero spread among the losses is
    # not zero downside. Reported `None` before the fix.
    metrics = _metrics([5000, -2000, 4000, -2000, 6000, -2000])
    assert metrics.sortino is not None
    assert metrics.sortino > 0


# --- the denominator itself -----------------------------------------------


def test_downside_deviation_counts_winning_periods_as_zero_downside():
    from app.backtest.metrics import _downside_deviation

    # Dividing by only the losers would make a strategy look better the
    # more often it won -- the opposite of what the ratio measures. Two
    # series with the same single loss and different win counts must give
    # different downside deviations.
    assert _downside_deviation([0.02, -0.02]) == pytest.approx(0.02 / math.sqrt(2))
    assert _downside_deviation([0.02, 0.02, 0.02, -0.02]) == pytest.approx(0.01)


def test_downside_deviation_ignores_how_the_gains_are_shaped():
    from app.backtest.metrics import _downside_deviation

    # Control: only the shortfalls below the target contribute at all.
    assert _downside_deviation([0.05, -0.01]) == _downside_deviation([0.90, -0.01])


def test_downside_deviation_is_zero_when_nothing_lost():
    from app.backtest.metrics import _downside_deviation

    assert _downside_deviation([0.01, 0.02, 0.0]) == 0.0
    assert _downside_deviation([]) == 0.0


def test_a_run_with_no_losing_trade_reports_no_sortino():
    # `None`, never an infinity: `backtest_metrics.sortino` is
    # Numeric(10, 4) and cannot hold one, and the replay statistics that
    # travel beside it go into a Postgres json column that rejects the
    # bare `Infinity` token.
    metrics = _metrics([5000, 4000, 6000])
    assert metrics.sortino is None


# --- sharpe, unchanged, as the control ------------------------------------


def test_sharpe_is_the_sample_deviation_of_every_return():
    # Control: this PR does not touch `sharpe`, and it must still be the
    # ordinary sample-standard-deviation ratio over all returns. If a
    # future change makes the two ratios share a denominator by accident,
    # this fails.
    pnls = [5000, -2000, 4000, -2000, 6000, -2000]
    returns = [p / CAPITAL for p in pnls]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    assert _metrics(pnls).sharpe == pytest.approx(mean / math.sqrt(variance), abs=5e-5)


def test_sortino_rewards_a_run_whose_volatility_is_mostly_upside():
    # The two ratios differ because Sortino's denominator ignores upside
    # dispersion entirely. So a run with one big win and one equal-sized
    # loss scores *better* on Sortino than on Sharpe: the win inflates
    # Sharpe's denominator and contributes nothing to Sortino's.
    #
    # (Written this way after a first draft asserted the opposite --
    # "rare large losses should score worse on Sortino" -- and measured
    # the other way round. Sharpe is penalised by the upside too.)
    metrics = _metrics([12000, 1000, 1000, 1000, -4000])
    assert metrics.sharpe is not None and metrics.sortino is not None
    assert metrics.sortino > metrics.sharpe
