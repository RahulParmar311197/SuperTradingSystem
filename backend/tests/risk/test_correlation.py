import pytest
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


def _closes_from_returns(start: float, returns: list[float]) -> list[float]:
    closes = [start]
    for r in returns:
        closes.append(closes[-1] * (1 + r))
    return closes


def _timestamped(closes: list[float], step_minutes: int = 1, offset: int = 0) -> dict:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return {start + timedelta(minutes=offset + i * step_minutes): c for i, c in enumerate(closes)}


def test_build_correlation_matrix_covers_every_pair():
    # BANKNIFTY's returns are exactly 2x NIFTY's, so the pair correlates 1.0.
    closes_by_symbol = {
        "NIFTY": _timestamped(_closes_from_returns(100, [0.01, 0.02, -0.01, 0.03])),
        "BANKNIFTY": _timestamped(_closes_from_returns(100, [0.02, 0.04, -0.02, 0.06])),
        "GOLD": _timestamped(_closes_from_returns(100, [0.01, -0.03, 0.02, -0.01])),
    }
    matrix = build_correlation_matrix(closes_by_symbol)
    assert matrix[frozenset(("NIFTY", "BANKNIFTY"))] == pytest.approx(1.0)
    assert frozenset(("NIFTY", "GOLD")) in matrix


def test_correlation_aligns_series_on_shared_timestamps():
    # Regression test: `build_correlation_matrix` used to take bare return
    # lists and `pearson_correlation` aligned them by list position, taking
    # the last N of each. Two instruments' histories are not interchangeable
    # by position -- an illiquid symbol prints fewer bars over the same
    # wall-clock span -- so that compared one instrument's recent history
    # against another's older history and reported a meaningless number.
    #
    # Both symbols here follow the *identical* price path at the *identical*
    # timestamps, so they are perfectly correlated in real time. The only
    # difference is that ILLIQ prints every other bar. Position-based
    # alignment reported -0.79 for this pair; timestamp alignment gives 1.0.
    path = [100 + (i % 2) * 2 for i in range(60)] + [100 + i for i in range(60)]
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    liquid = {start + timedelta(minutes=i): path[i] for i in range(120)}
    illiquid = {start + timedelta(minutes=i): path[i] for i in range(0, 120, 2)}

    matrix = build_correlation_matrix({"LIQUID": liquid, "ILLIQ": illiquid})
    assert matrix[frozenset(("LIQUID", "ILLIQ"))] == pytest.approx(1.0)

    # Pin the harm the old behaviour actually caused, so this cannot regress
    # into "passes because the API changed". Correlating the two return
    # series by list position -- which is what the old code did, taking the
    # last N of each -- reports a strongly *negative* correlation for two
    # instruments that move identically.
    def _series_returns(series: dict) -> list[float]:
        closes = [series[t] for t in sorted(series)]
        return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]

    naive = pearson_correlation(_series_returns(liquid), _series_returns(illiquid))
    assert naive < -0.5
    assert abs(naive - 1.0) > 1.5  # nowhere near the true correlation


def test_build_correlation_matrix_skips_pairs_with_too_little_overlap():
    # Two symbols whose histories barely intersect have no computable
    # correlation -- they must be omitted entirely rather than correlated
    # across the periods they do not share.
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    early = {start + timedelta(minutes=i): 100 + i for i in range(10)}
    late = {start + timedelta(minutes=i): 100 + i for i in range(9, 20)}

    matrix = build_correlation_matrix({"EARLY": early, "LATE": late})
    assert frozenset(("EARLY", "LATE")) not in matrix


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
