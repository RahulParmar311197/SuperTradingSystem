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

from app.smc.types import Candle


def close_returns(candles: list[Candle]) -> list[float]:
    """Simple close-to-close returns, oldest first."""
    closes = [c.close for c in candles]
    return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] != 0]


def pearson_correlation(a: list[float], b: list[float]) -> float | None:
    """Pearson correlation of two equal-length return series. Returns
    `None` when there isn't enough data to say anything (fewer than 2
    points, or a series with zero variance) rather than a misleading 0.0."""
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


def build_correlation_matrix(returns_by_symbol: dict[str, list[float]]) -> dict[frozenset[str], float]:
    """Pairwise correlation for every symbol pair with computable data."""
    symbols = list(returns_by_symbol)
    matrix: dict[frozenset[str], float] = {}
    for i, sym_a in enumerate(symbols):
        for sym_b in symbols[i + 1 :]:
            corr = pearson_correlation(returns_by_symbol[sym_a], returns_by_symbol[sym_b])
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
