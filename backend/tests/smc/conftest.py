from datetime import datetime, timedelta, timezone

from app.smc.types import Candle


def make_candles(ohlc: list[tuple[float, float, float, float]], volume: float = 100.0) -> list[Candle]:
    """Builds candles one minute apart from a list of (open, high, low, close) tuples."""
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)  # a Monday
    return [
        Candle(
            timestamp=start + timedelta(minutes=i),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=volume,
        )
        for i, (o, h, l, c) in enumerate(ohlc)
    ]
