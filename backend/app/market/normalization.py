import math
from datetime import datetime, timezone
from typing import Any, Mapping

from .models import MarketEvent, Timeframe


class InvalidMarketEvent(ValueError):
    """Raised when a raw payload cannot be safely normalized into a MarketEvent."""


def _require_finite(name: str, value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise InvalidMarketEvent(f"{name} is not numeric: {value!r}") from exc
    if not math.isfinite(number):
        raise InvalidMarketEvent(f"{name} is not finite: {value!r}")
    return number


def normalize_candle(raw: Mapping[str, Any]) -> MarketEvent:
    """Normalize a raw provider candle payload into a canonical MarketEvent.

    Fails closed: any structurally invalid or economically impossible
    candle (e.g. high below low, negative volume, non-finite price) is
    rejected rather than silently passed downstream, since the SMC engine
    and strategy layer assume clean OHLC data.
    """

    try:
        symbol = str(raw["symbol"]).strip()
    except KeyError as exc:
        raise InvalidMarketEvent("missing symbol") from exc
    if not symbol:
        raise InvalidMarketEvent("symbol must not be empty")

    try:
        timeframe = Timeframe(raw["timeframe"])
    except KeyError as exc:
        raise InvalidMarketEvent("missing timeframe") from exc
    except ValueError as exc:
        raise InvalidMarketEvent(f"unsupported timeframe: {raw['timeframe']!r}") from exc

    timestamp = raw.get("timestamp")
    if timestamp is None:
        raise InvalidMarketEvent("missing timestamp")
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidMarketEvent(f"unparseable timestamp: {timestamp!r}") from exc
    if not isinstance(timestamp, datetime):
        raise InvalidMarketEvent(f"timestamp must be a datetime or ISO string: {timestamp!r}")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    open_ = _require_finite("open", raw.get("open"))
    high = _require_finite("high", raw.get("high"))
    low = _require_finite("low", raw.get("low"))
    close = _require_finite("close", raw.get("close"))
    volume = _require_finite("volume", raw.get("volume", 0))

    if low > high:
        raise InvalidMarketEvent(f"low ({low}) is greater than high ({high})")
    if not (low <= open_ <= high):
        raise InvalidMarketEvent(f"open ({open_}) outside [low, high] = [{low}, {high}]")
    if not (low <= close <= high):
        raise InvalidMarketEvent(f"close ({close}) outside [low, high] = [{low}, {high}]")
    if volume < 0:
        raise InvalidMarketEvent(f"volume must not be negative: {volume}")
    if open_ <= 0 or high <= 0 or low <= 0 or close <= 0:
        raise InvalidMarketEvent("prices must be positive")

    return MarketEvent(
        symbol=symbol,
        timeframe=timeframe,
        timestamp=timestamp,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )
