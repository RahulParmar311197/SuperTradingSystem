from datetime import datetime, timedelta, timezone

from app.market.models import MarketEvent, Timeframe
from app.smc.models import SwingKind, SwingLabel
from app.smc.swings import detect_swings


def make_candles(highs: list[float], lows: list[float], closes: list[float] | None = None) -> list[MarketEvent]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    closes = closes or [(h + l) / 2 for h, l in zip(highs, lows)]
    return [
        MarketEvent(
            symbol="TEST",
            timeframe=Timeframe.M15,
            timestamp=start + timedelta(minutes=15 * i),
            open=(h + l) / 2,
            high=h,
            low=l,
            close=c,
            volume=1,
        )
        for i, (h, l, c) in enumerate(zip(highs, lows, closes))
    ]


def test_detects_swing_high_and_low():
    highs = [10, 15, 11, 9, 8, 20]
    lows = [9, 13, 9, 7, 6, 18]
    candles = make_candles(highs, lows)

    swings = detect_swings(candles, swing_length=1)

    assert len(swings) == 2
    high_swing = next(s for s in swings if s.kind is SwingKind.HIGH)
    low_swing = next(s for s in swings if s.kind is SwingKind.LOW)
    assert high_swing.index == 1 and high_swing.price == 15
    assert low_swing.index == 4 and low_swing.price == 6


def test_labels_higher_high_and_higher_low():
    # Two clear swing highs (15 then 20), detectable with swing_length=1.
    highs = [10, 15, 9, 20, 8]
    lows = [9, 13, 7, 18, 6]
    candles = make_candles(highs, lows)

    swings = detect_swings(candles, swing_length=1)
    highs_found = [s for s in swings if s.kind is SwingKind.HIGH]

    assert len(highs_found) == 2
    assert highs_found[0].label is None  # nothing to compare the first swing against
    assert highs_found[1].label is SwingLabel.HH


def test_no_swings_when_history_too_short():
    candles = make_candles([10, 11], [9, 10])
    assert detect_swings(candles, swing_length=3) == []


def test_swing_length_must_be_positive():
    import pytest

    with pytest.raises(ValueError):
        detect_swings(make_candles([10], [9]), swing_length=0)
