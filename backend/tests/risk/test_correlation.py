from app.risk.correlation import build_correlation_matrix, close_returns, correlated_exposure, pearson_correlation
from app.smc.types import Candle
from datetime import datetime, timedelta, timezone


def _candles(closes: list[float]) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return [Candle(start + timedelta(minutes=i), c, c, c, c, 100) for i, c in enumerate(closes)]


def test_close_returns_computes_simple_returns():
    returns = close_returns(_candles([100, 110, 99]))
    assert returns == [0.1, (99 - 110) / 110]


def test_pearson_correlation_perfectly_correlated_series():
    a = [0.01, 0.02, -0.01, 0.03, -0.02]
    b = [0.02, 0.04, -0.02, 0.06, -0.04]  # exactly 2x a
    assert pearson_correlation(a, b) == 1.0


def test_pearson_correlation_perfectly_anti_correlated_series():
    a = [0.01, 0.02, -0.01, 0.03, -0.02]
    b = [-x for x in a]
    assert pearson_correlation(a, b) == -1.0


def test_pearson_correlation_none_with_insufficient_data():
    assert pearson_correlation([0.01], [0.02]) is None
    assert pearson_correlation([], []) is None


def test_pearson_correlation_none_with_zero_variance():
    assert pearson_correlation([0.01, 0.01, 0.01], [0.02, 0.03, -0.01]) is None


def test_build_correlation_matrix_covers_every_pair():
    returns = {
        "NIFTY": [0.01, 0.02, -0.01, 0.03],
        "BANKNIFTY": [0.02, 0.04, -0.02, 0.06],
        "GOLD": [0.01, -0.03, 0.02, -0.01],
    }
    matrix = build_correlation_matrix(returns)
    assert matrix[frozenset(("NIFTY", "BANKNIFTY"))] == 1.0
    assert frozenset(("NIFTY", "GOLD")) in matrix


def test_correlated_exposure_sums_only_positions_above_threshold():
    matrix = {
        frozenset(("NIFTY", "BANKNIFTY")): 0.9,
        frozenset(("NIFTY", "GOLD")): 0.1,
    }
    total = correlated_exposure(
        target_symbol="NIFTY",
        target_notional=1000.0,
        open_position_notionals={"BANKNIFTY": 500.0, "GOLD": 2000.0},
        correlation_matrix=matrix,
        threshold=0.7,
    )
    assert total == 1500.0  # NIFTY + BANKNIFTY, GOLD excluded (corr below threshold)


def test_correlated_exposure_ignores_pairs_with_no_computed_correlation():
    total = correlated_exposure(
        target_symbol="NIFTY",
        target_notional=1000.0,
        open_position_notionals={"UNKNOWN": 5000.0},
        correlation_matrix={},
        threshold=0.7,
    )
    assert total == 1000.0
