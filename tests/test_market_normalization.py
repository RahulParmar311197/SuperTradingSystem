import pytest

from app.market.normalization import InvalidMarketEvent, normalize_candle


def _raw(**overrides) -> dict:
    base = {
        "symbol": "NIFTY",
        "timeframe": "15m",
        "timestamp": "2026-08-21T10:15:00Z",
        "open": 25000,
        "high": 25050,
        "low": 24980,
        "close": 25030,
        "volume": 100000,
    }
    base.update(overrides)
    return base


def test_normalize_valid_candle():
    event = normalize_candle(_raw())
    assert event.symbol == "NIFTY"
    assert event.high == 25050
    assert event.timestamp.tzinfo is not None


def test_rejects_missing_symbol():
    raw = _raw()
    del raw["symbol"]
    with pytest.raises(InvalidMarketEvent):
        normalize_candle(raw)


def test_rejects_high_below_low():
    with pytest.raises(InvalidMarketEvent):
        normalize_candle(_raw(high=100, low=200))


def test_rejects_open_outside_range():
    with pytest.raises(InvalidMarketEvent):
        normalize_candle(_raw(open=99999))


def test_rejects_negative_volume():
    with pytest.raises(InvalidMarketEvent):
        normalize_candle(_raw(volume=-1))


def test_rejects_non_finite_price():
    with pytest.raises(InvalidMarketEvent):
        normalize_candle(_raw(close=float("nan")))


def test_rejects_unsupported_timeframe():
    with pytest.raises(InvalidMarketEvent):
        normalize_candle(_raw(timeframe="7m"))
