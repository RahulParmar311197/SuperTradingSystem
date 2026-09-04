from app.market.models import MarketEvent

from .models import Swing, SwingKind, SwingLabel


def detect_swings(candles: list[MarketEvent], swing_length: int = 3) -> list[Swing]:
    """Detect confirmed fractal swing highs/lows (blueprint section 19).

    A candle at index i is a swing high if its high is the strict maximum
    among the `swing_length` candles on each side; a swing low is the
    symmetric case on lows.

    Look-ahead safety: a swing at index i requires `swing_length` candles
    *after* it to exist before it can be confirmed. This function only
    returns swings that are fully confirmed by the candles provided, so
    calling it with `candles[: t + 1]` during replay/live processing can
    never surface a swing whose confirmation depended on data beyond
    timestamp t.
    """

    if swing_length < 1:
        raise ValueError("swing_length must be >= 1")

    swings: list[Swing] = []
    n = len(candles)

    for i in range(swing_length, n - swing_length):
        window = candles[i - swing_length : i + swing_length + 1]
        candle = candles[i]

        is_high = all(candle.high >= other.high for other in window) and any(
            candle.high > other.high for other in window if other is not candle
        )
        is_low = all(candle.low <= other.low for other in window) and any(
            candle.low < other.low for other in window if other is not candle
        )

        if is_high:
            swings.append(Swing(index=i, kind=SwingKind.HIGH, price=candle.high, candle=candle))
        if is_low:
            swings.append(Swing(index=i, kind=SwingKind.LOW, price=candle.low, candle=candle))

    _label_swings(swings)
    return swings


def _label_swings(swings: list[Swing]) -> None:
    """Classify each swing as HH/HL/LH/LL relative to the prior swing of the same kind."""

    last_high: Swing | None = None
    last_low: Swing | None = None

    for swing in swings:
        if swing.kind is SwingKind.HIGH:
            if last_high is not None:
                swing.label = SwingLabel.HH if swing.price > last_high.price else SwingLabel.LH
            last_high = swing
        else:
            if last_low is not None:
                swing.label = SwingLabel.HL if swing.price > last_low.price else SwingLabel.LL
            last_low = swing
