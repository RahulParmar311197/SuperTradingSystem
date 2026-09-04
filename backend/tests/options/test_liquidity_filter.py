from datetime import datetime, timedelta, timezone

from app.options.liquidity_filter import LiquidityFilterConfig, evaluate_liquidity


def test_accepts_liquid_contract():
    now = datetime.now(timezone.utc)
    result = evaluate_liquidity(
        volume=1000, open_interest=5000, bid=99.0, ask=101.0, quote_timestamp=now, now=now
    )
    assert result.acceptable is True
    assert result.rejections == []


def test_rejects_low_volume_and_oi():
    now = datetime.now(timezone.utc)
    config = LiquidityFilterConfig(min_volume=100, min_open_interest=500)
    result = evaluate_liquidity(volume=5, open_interest=10, bid=99, ask=101, quote_timestamp=now, config=config, now=now)
    assert result.acceptable is False
    assert len(result.rejections) == 2


def test_rejects_wide_spread():
    now = datetime.now(timezone.utc)
    result = evaluate_liquidity(volume=1000, open_interest=5000, bid=80.0, ask=120.0, quote_timestamp=now, now=now)
    assert result.acceptable is False


def test_rejects_stale_quote():
    now = datetime.now(timezone.utc)
    stale = now - timedelta(seconds=120)
    result = evaluate_liquidity(volume=1000, open_interest=5000, bid=99, ask=101, quote_timestamp=stale, now=now)
    assert result.acceptable is False
