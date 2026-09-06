"""Correlation engine (blueprint §85): tracks correlations between
instruments from real historical closes (§14's already-persisted candle
pipeline — no synthetic or assumed correlation data), and tells the risk
engine when a new position would concentrate exposure across instruments
that tend to move together.

`"The system can reject a new position when aggregate correlated exposure
is too high"` (§85) is enforced as one more `RiskCheck` in
`app.risk.engine.RiskEngine`, not here — this module only computes the
numbers.
"""

from __future__ import annotations

from datetime import datetime

from app.smc.types import Candle


def _returns(closes: list[float]) -> list[float]:
    """Simple close-to-close returns over an already-ordered close series."""
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] != 0]


def close_returns(candles: list[Candle]) -> list[float]:
    """Simple close-to-close returns, oldest first."""
    return _returns([c.close for c in candles])


def closes_by_timestamp(candles: list[Candle]) -> dict[datetime, float]:
    """Closes keyed by candle timestamp, so two instruments' series can be
    aligned on the bars they actually share before being correlated."""
    return {c.timestamp: c.close for c in candles}


def pearson_correlation(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of two return series that are already aligned
    point-for-point — aligning them is the caller's job (see
    `build_correlation_matrix`, which intersects on timestamps first).
    Returns `None` when there isn't enough data to say anything (fewer
    than 2 points, or a series with zero variance) rather than a
    misleading 0.0.

    Series of differing length are truncated from the tail purely as a
    defensive fallback; correlating unaligned series that way is
    meaningless, which is exactly the bug `build_correlation_matrix` now
    prevents upstream."""
    n = min(len(a), len(b))
    if n < 2:
        return None
    a, b = a[-n:], b[-n:]

    mean_a = sum(a) / n
    mean_b = sum(b) / n
    cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
    var_a = sum((x - mean_a) ** 2 for x in a)
    var_b = sum((y - mean_b) ** 2 for y in b)
    if var_a == 0 or var_b == 0:
        return None
    return cov / (var_a * var_b) ** 0.5


def build_correlation_matrix(closes_by_symbol: dict[str, dict[datetime, float]]) -> dict[frozenset[str], float]:
    """Pairwise correlation for every symbol pair with computable data.

    Takes closes keyed by timestamp, not bare return lists, because two
    instruments' histories are not interchangeable by position: an
    illiquid symbol prints fewer bars over the same wall-clock span, and a
    symbol listed later simply starts later. Each pair is intersected on
    the timestamps both actually have, *then* returns are computed over
    that shared series, so every pair of points being correlated covers
    the same interval. Correlating by list position instead — taking the
    last N of each — silently compares one instrument's recent history
    against another's older history and reports a number with no meaning.
    """
    symbols = list(closes_by_symbol)
    matrix: dict[frozenset[str], float] = {}
    for i, sym_a in enumerate(symbols):
        for sym_b in symbols[i + 1 :]:
            closes_a, closes_b = closes_by_symbol[sym_a], closes_by_symbol[sym_b]
            shared = sorted(set(closes_a) & set(closes_b))
            if len(shared) < 3:  # need >= 3 closes to get >= 2 returns
                continue
            corr = pearson_correlation(
                _returns([closes_a[t] for t in shared]), _returns([closes_b[t] for t in shared])
            )
            if corr is not None:
                matrix[frozenset((sym_a, sym_b))] = corr
    return matrix


def correlated_exposure(
    target_symbol: str,
    target_notional: float,
    open_position_notionals: dict[str, float],
    correlation_matrix: dict[frozenset[str], float],
    threshold: float,
) -> float:
    """Notional of `target_symbol`'s new position plus every open
    position whose |correlation| with it is >= `threshold`. A pair with
    no entry in `correlation_matrix` (no computable correlation) is
    treated as uncorrelated — this only flags concentration it actually
    has evidence for."""
    total = target_notional
    for symbol, notional in open_position_notionals.items():
        if symbol == target_symbol:
            continue
        corr = correlation_matrix.get(frozenset((target_symbol, symbol)))
        if corr is not None and abs(corr) >= threshold:
            total += abs(notional)
    return total
