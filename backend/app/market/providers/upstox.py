"""Read-only Upstox market data (blueprint §14, §52).

**This class is deliberately not a `Broker`.** It does not subclass it, and
it has no `place_order`, `modify_order` or `cancel_order` — not as a policy
that could be forgotten, but structurally: there is no method here that
could place an order, so the credential it holds cannot reach an exchange
through this object.

That matters because of how execution is resolved. `resolve_broker` picks
the most recent ACTIVE `BrokerAccount` for every order a user places, and
`_execution_mode_for` stamps anything that is not a `MockBroker` as LIVE.
So connecting an Upstox account merely to obtain a market-data token would
also route that user's real orders to Upstox. Market data is therefore
configured process-wide (`UPSTOX_DATA_ACCESS_TOKEN`), creates no
`BrokerAccount` row, and is consumed only through this class -- which
leaves `resolve_broker` returning `MockBroker` and execution in paper.

A caution worth repeating where the token is handled: Upstox does not
issue read-only market-data tokens. The value this class holds *can* place
orders at Upstox through any other client. What this module guarantees is
that *this system* offers no path from it to an order, not that the
credential is harmless if it leaks.

Like `app.brokers.upstox.adapter`, this is written against Upstox's
documented v2 shapes and **has not been confirmed against live servers** --
this environment cannot reach them. Response parsing is deliberately
isolated in `parse_candles` / `parse_ltp` so it can be fixture-tested now
and corrected from one real call later, which is what blueprint §120 asks
for.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx

from app.brokers.base import BrokerError
from app.smc.types import Candle

logger = logging.getLogger("market.providers.upstox")

UPSTOX_API_BASE_URL = "https://api.upstox.com/v2"

# Upstox's documented historical intervals. Kept as an explicit set so an
# unsupported one fails here, with the caller's value named, instead of as
# an opaque 400 from the far end.
HISTORICAL_INTERVALS = frozenset({"1minute", "30minute", "day", "week", "month"})
INTRADAY_INTERVALS = frozenset({"1minute", "30minute"})


def parse_ltp(payload: dict, instrument_key: str) -> float | None:
    """Last traded price out of `/market-quote/ltp`'s `data` envelope.

    Upstox keys that map by a *response* symbol ("NSE_EQ:INFY") rather than
    by the instrument_key the request asked for, and the exact spelling has
    moved between versions. Rather than guess the key, take the single
    entry when there is exactly one -- which is what a one-instrument
    request returns -- and only then fall back to matching.
    """
    data = payload.get("data") or {}
    if not data:
        return None
    if len(data) == 1:
        entry = next(iter(data.values()))
    else:
        entry = next(
            (v for k, v in data.items() if instrument_key in k or k in instrument_key),
            None,
        )
    if not isinstance(entry, dict):
        return None
    ltp = entry.get("last_price", entry.get("ltp"))
    return float(ltp) if ltp is not None else None


def parse_candles(payload: dict) -> list[Candle]:
    """Upstox historical candles, normalised to this codebase's conventions.

    Two conversions here are load-bearing, and both are the kind of thing
    that fails silently rather than loudly:

    * **Order.** Upstox returns candles newest-first. Every consumer in
      this codebase -- `detect_swings`, `bucket_start`, the paper engine's
      `candles[-1]`, the backtest loop -- assumes oldest-first. Handing
      them a reversed series would not raise; it would just produce
      confident nonsense. Sorted ascending explicitly.
    * **Timezone.** Upstox stamps candles in IST (`+05:30`). This codebase
      is UTC throughout, and two separate bugs have already come from an
      IST clock time reaching a UTC reader (kill zones read the candle's
      local hour; the ICT session open was an IST literal in a UTC slot).
      Converted to UTC here, at the boundary, so nothing downstream has to
      know Upstox exists.

    Each row is `[timestamp, open, high, low, close, volume, open_interest]`.
    Open interest is dropped: `Candle` has nowhere to put it, and inventing
    a field the SMC engine would not read is worse than leaving it out.
    """
    rows = (payload.get("data") or {}).get("candles") or []
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            logger.warning("Skipping malformed Upstox candle row: %r", row)
            continue
        try:
            stamped = datetime.fromisoformat(str(row[0]))
        except ValueError:
            logger.warning("Skipping Upstox candle with unparseable timestamp: %r", row[0])
            continue
        # A naive timestamp is read as UTC rather than passed to
        # `astimezone()`, which would silently assume the *machine's* zone
        # -- the same mistake one layer down, and one a UTC-configured CI
        # could never catch.
        utc = stamped.replace(tzinfo=timezone.utc) if stamped.tzinfo is None else stamped.astimezone(timezone.utc)
        candles.append(
            Candle(
                timestamp=utc,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
            )
        )
    candles.sort(key=lambda c: c.timestamp)
    return candles


class UpstoxMarketData:
    """Quotes and historical candles. No ordering surface — see the module
    docstring for why that is structural rather than a convention."""

    def __init__(self, access_token: str, http_client: httpx.AsyncClient | None = None) -> None:
        if not access_token:
            raise ValueError("UpstoxMarketData needs an access token")
        self._access_token = access_token
        self._http = http_client or httpx.AsyncClient(timeout=15.0)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"}

    async def _get(self, path: str, **kwargs) -> dict:
        """GET, with the broker's own words surfaced rather than a bare status.

        Raises `BrokerError` rather than returning a sentinel: unlike
        `place_order`, whose contract forbids raising because the caller has
        already registered an order by then, nothing irreversible has
        happened here. A caller that cannot get data should see why.
        """
        try:
            response = await self._http.get(f"{UPSTOX_API_BASE_URL}{path}", headers=self._headers(), **kwargs)
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise BrokerError(f"Upstox market data {path} failed: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise BrokerError(f"Upstox market data {path} failed: {type(exc).__name__}: {exc}") from exc
        if payload.get("status") == "error":
            errors = payload.get("errors") or [{"message": payload.get("message", "unknown error")}]
            raise BrokerError("; ".join(e.get("message", str(e)) for e in errors))
        return payload

    async def get_ltp(self, instrument_key: str) -> float | None:
        payload = await self._get("/market-quote/ltp", params={"instrument_key": instrument_key})
        return parse_ltp(payload, instrument_key)

    async def get_historical_candles(
        self, instrument_key: str, interval: str, from_date: date, to_date: date
    ) -> list[Candle]:
        if interval not in HISTORICAL_INTERVALS:
            raise ValueError(f"interval {interval!r} is not one of {sorted(HISTORICAL_INTERVALS)}")
        if from_date > to_date:
            raise ValueError(f"from_date {from_date} is after to_date {to_date}")
        payload = await self._get(
            f"/historical-candle/{instrument_key}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
        )
        return parse_candles(payload)

    async def get_intraday_candles(self, instrument_key: str, interval: str) -> list[Candle]:
        """Today's candles so far. Separate endpoint at Upstox: the
        historical one does not include the current, still-forming day."""
        if interval not in INTRADAY_INTERVALS:
            raise ValueError(f"interval {interval!r} is not one of {sorted(INTRADAY_INTERVALS)}")
        payload = await self._get(f"/historical-candle/intraday/{instrument_key}/{interval}")
        return parse_candles(payload)

    async def aclose(self) -> None:
        await self._http.aclose()
