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
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """One side (call or put) of one strike, as the chain reports it.

    Every price field is optional because Upstox omits rather than zeroes
    what it has no value for, and a contract that has not traded today
    genuinely has no LTP. `volume` and `open_interest` default to 0.0
    instead: those are counts over the session, and "no trades" really is
    zero — which is exactly the reading
    `app.options.liquidity_filter.evaluate_liquidity` needs to reject a
    dead strike rather than skip it.
    """

    instrument_key: str
    strike: float
    option_type: str  # "CE" or "PE"
    ltp: float | None = None
    bid: float | None = None
    ask: float | None = None
    volume: float = 0.0
    open_interest: float = 0.0
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None


@dataclass(frozen=True, slots=True)
class OptionChain:
    underlying_spot_price: float | None
    quotes: list[OptionQuote]


def _opt_float(value) -> float | None:
    """A float, or None for anything that is not a usable number.

    Upstox sends `null`, `""` and occasionally `"NA"` in price fields.
    `float("")` raises, and a raised ValueError several layers into an
    ingestion loop would discard a whole chain over one bad strike.
    """
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_one_side(entry: dict, strike: float, option_type: str) -> OptionQuote | None:
    instrument_key = (entry.get("instrument_key") or "").strip()
    if not instrument_key:
        return None
    market = entry.get("market_data") or {}
    greeks = entry.get("option_greeks") or {}
    return OptionQuote(
        instrument_key=instrument_key,
        strike=strike,
        option_type=option_type,
        ltp=_opt_float(market.get("ltp")),
        bid=_opt_float(market.get("bid_price")),
        ask=_opt_float(market.get("ask_price")),
        # `or 0.0` and not `_opt_float(...)`: see OptionQuote's docstring.
        volume=_opt_float(market.get("volume")) or 0.0,
        open_interest=_opt_float(market.get("oi")) or 0.0,
        iv=_opt_float(greeks.get("iv")),
        delta=_opt_float(greeks.get("delta")),
        gamma=_opt_float(greeks.get("gamma")),
        theta=_opt_float(greeks.get("theta")),
        vega=_opt_float(greeks.get("vega")),
    )


def parse_option_chain(payload: dict) -> OptionChain:
    """Upstox `/option/chain`, flattened to one quote per tradable side.

    The documented shape is a list of per-strike rows, each carrying a
    `strike_price`, an `underlying_spot_price` and up to two nested sides
    (`call_options`, `put_options`). A row missing its strike is skipped
    rather than stored at 0.0, which would be a real strike at the money
    for a low-priced underlying.

    Isolated as a pure function for the same reason `parse_candles` is:
    this environment cannot reach Upstox's servers, so the shape is
    fixture-tested here and correctable from one real call later
    (blueprint §120).
    """
    rows = payload.get("data") or []
    spot: float | None = None
    quotes: list[OptionQuote] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if spot is None:
            spot = _opt_float(row.get("underlying_spot_price"))
        strike = _opt_float(row.get("strike_price"))
        if strike is None:
            logger.warning("Skipping an option-chain row with no strike_price: %s", sorted(row))
            continue
        for key, option_type in (("call_options", "CE"), ("put_options", "PE")):
            side = row.get(key)
            if not isinstance(side, dict):
                continue
            quote = _parse_one_side(side, strike, option_type)
            if quote is not None:
                quotes.append(quote)
    return OptionChain(underlying_spot_price=spot, quotes=quotes)


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

    async def get_option_chain(self, instrument_key: str, expiry: date) -> OptionChain:
        """The full chain for one underlying and one expiry.

        `instrument_key` names the *underlying* here (e.g. the Nifty 50
        index), not a contract — the response is what enumerates the
        contracts.
        """
        payload = await self._get(
            "/option/chain",
            params={"instrument_key": instrument_key, "expiry_date": expiry.isoformat()},
        )
        return parse_option_chain(payload)

    async def aclose(self) -> None:
        await self._http.aclose()
