from datetime import datetime, timezone

from app.market.normalization import normalize_tick

FIELD_MAP = {
    "timestamp": "ts",
    "ltp": "last_price",
    "volume": "vol_traded_today",
    "bid": "best_bid_price",
    "ask": "best_ask_price",
    "open_interest": "oi",
}


def test_normalizes_raw_broker_payload():
    raw = {
        "ts": datetime(2026, 8, 21, 10, 15, tzinfo=timezone.utc),
        "last_price": 25030.0,
        "vol_traded_today": 100000,
        "best_bid_price": 25028.0,
        "best_ask_price": 25032.0,
        "oi": 1500000,
    }

    tick = normalize_tick(raw, FIELD_MAP, symbol="NIFTY", exchange="NSE", market="EQUITY")

    assert tick.symbol == "NIFTY"
    assert tick.ltp == 25030.0
    assert tick.spread == 4.0
    assert tick.open_interest == 1500000


def test_missing_ltp_mapping_raises():
    import pytest

    with pytest.raises(ValueError):
        normalize_tick(
            {"ts": datetime.now(timezone.utc)},
            {"timestamp": "ts"},
            symbol="NIFTY",
            exchange="NSE",
            market="EQUITY",
        )
