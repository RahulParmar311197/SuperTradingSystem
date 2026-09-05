"""Upstox broker adapter (blueprint §52).

Implemented against Upstox's public developer documentation and community
posts as of 2026-09 — this sandbox's network egress to upstox.com is
blocked, so the actual doc pages could not be fetched and cross-checked
directly; this is built from search-result snippets plus the well-known
shape of Upstox's v2 API (the `{"status": "success", "data": ...}`
envelope, the OAuth2 authorization-code flow). **Before connecting a real
account, verify every endpoint, field name, and status string below
against the live docs or Postman collection** — this is exactly the kind
of drift the blueprint's "always use current official docs" instruction
(§52/§120) is warning about; treat this file as a strong first draft, not
a certified implementation.

Rollout checklist before live trading (blueprint §120):
  1. OAuth authentication — see app.brokers.upstox.oauth.
  2. Verify market data / instrument lookup (this adapter expects the
     caller to pass Upstox "instrument_key" strings, e.g.
     "NSE_EQ|INE669E01016", as the `symbol` — there is no symbol->key
     resolution here yet; add one against Upstox's instrument master CSV
     before relying on plain trading symbols).
  3. Verify quotes and the option chain + Greeks endpoint response shapes.
  4. Verify place/modify/cancel order field names and status strings.
  5. Verify positions/funds response shapes.
  6. Test each of the above against Upstox's sandbox, if available.
  7. Test disconnect/reconnect handling.
  8. Only then flip this account out of paper mode.
"""

from __future__ import annotations

import logging

import httpx

from app.brokers.base import (
    AccountInfo,
    Broker,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderRequest,
    OrderResult,
    Quote,
)
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType

logger = logging.getLogger("brokers.upstox")

UPSTOX_API_BASE_URL = "https://api.upstox.com/v2"
# Order placement is documented on a separate low-latency host as of the
# "Uplink API v2" changes — verify this still holds before relying on it.
UPSTOX_ORDER_BASE_URL = "https://api-hft.upstox.com/v2"

_DIRECTION_TO_TRANSACTION_TYPE = {Direction.LONG: "BUY", Direction.SHORT: "SELL"}
_TRANSACTION_TYPE_TO_DIRECTION = {v: k for k, v in _DIRECTION_TO_TRANSACTION_TYPE.items()}

_ORDER_TYPE_TO_UPSTOX = {
    OrderType.MARKET: "MARKET",
    OrderType.LIMIT: "LIMIT",
    OrderType.SL: "SL",
    OrderType.SL_M: "SL-M",
}
_UPSTOX_TO_ORDER_TYPE = {v: k for k, v in _ORDER_TYPE_TO_UPSTOX.items()}

# Upstox order-book status strings, lowercased, mapped onto our state
# machine (app.database.models.trading.OrderStatus). Unrecognized statuses
# fall back to ACKNOWLEDGED with a logged warning rather than raising, so
# an unexpected new status string doesn't crash order tracking.
_UPSTOX_STATUS_TO_ORDER_STATUS = {
    "complete": OrderStatus.FILLED,
    "open": OrderStatus.ACKNOWLEDGED,
    "trigger pending": OrderStatus.ACKNOWLEDGED,
    "modify pending": OrderStatus.ACKNOWLEDGED,
    "modify after market order req received": OrderStatus.ACKNOWLEDGED,
    "cancel pending": OrderStatus.ACKNOWLEDGED,
    "open pending": OrderStatus.SUBMITTED,
    "validation pending": OrderStatus.SUBMITTED,
    "put order req received": OrderStatus.SUBMITTED,
    "cancelled": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


def _map_order_status(raw: str) -> OrderStatus:
    status = _UPSTOX_STATUS_TO_ORDER_STATUS.get((raw or "").strip().lower())
    if status is None:
        logger.warning("Unrecognized Upstox order status %r; treating as ACKNOWLEDGED", raw)
        return OrderStatus.ACKNOWLEDGED
    return status


def _unwrap(payload: dict) -> dict:
    """Upstox wraps most responses as {"status": "success"/"error", "data": ...}."""
    if payload.get("status") == "error":
        errors = payload.get("errors") or [{"message": payload.get("message", "Unknown Upstox error")}]
        raise BrokerError("; ".join(e.get("message", str(e)) for e in errors))
    return payload.get("data", payload)


class UpstoxBroker(Broker):
    def __init__(self, access_token: str, http_client: httpx.AsyncClient | None = None) -> None:
        self.access_token = access_token
        self._http = http_client or httpx.AsyncClient(timeout=10.0)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}

    async def _get(self, base_url: str, path: str, **kwargs) -> dict:
        response = await self._http.get(f"{base_url}{path}", headers=self._headers(), **kwargs)
        response.raise_for_status()
        return _unwrap(response.json())

    async def get_account(self) -> AccountInfo:
        data = await self._get(UPSTOX_API_BASE_URL, "/user/get-funds-and-margin")
        equity = data.get("equity", data)
        available = float(equity.get("available_margin", 0.0))
        used = float(equity.get("used_margin", 0.0))
        return AccountInfo(
            account_id="upstox",
            balance=available,
            equity=available + used,
            margin_used=used,
            margin_available=available,
        )

    async def get_positions(self) -> list[BrokerPosition]:
        data = await self._get(UPSTOX_API_BASE_URL, "/portfolio/short-term-positions")
        return [
            BrokerPosition(
                symbol=p.get("instrument_token", p.get("trading_symbol", "")),
                quantity=float(p.get("quantity", 0)),
                average_price=float(p.get("average_price", 0.0)),
                unrealized_pnl=float(p.get("unrealised", p.get("pnl", 0.0))),
            )
            for p in data
        ]

    async def get_orders(self) -> list[BrokerOrder]:
        data = await self._get(UPSTOX_API_BASE_URL, "/order/retrieve-all")
        orders: list[BrokerOrder] = []
        for o in data:
            direction = _TRANSACTION_TYPE_TO_DIRECTION.get(o.get("transaction_type", "BUY"), Direction.LONG)
            order_type = _UPSTOX_TO_ORDER_TYPE.get(o.get("order_type", "MARKET"), OrderType.MARKET)
            orders.append(
                BrokerOrder(
                    broker_order_id=o.get("order_id", ""),
                    symbol=o.get("instrument_token", o.get("trading_symbol", "")),
                    direction=direction,
                    order_type=order_type,
                    quantity=float(o.get("quantity", 0)),
                    price=float(o["price"]) if o.get("price") is not None else None,
                    status=_map_order_status(o.get("status", "")),
                    filled_quantity=float(o.get("filled_quantity", 0)),
                    average_fill_price=float(o["average_price"]) if o.get("average_price") else None,
                )
            )
        return orders

    async def get_quote(self, symbol: str) -> Quote:
        data = await self._get(UPSTOX_API_BASE_URL, "/market-quote/ltp", params={"instrument_key": symbol})
        entry = next(iter(data.values())) if data else {}
        return Quote(symbol=symbol, ltp=float(entry.get("last_price", 0.0)))

    async def get_option_chain(self, underlying: str, expiry: str) -> dict:
        return await self._get(
            UPSTOX_API_BASE_URL, "/option/chain", params={"instrument_key": underlying, "expiry_date": expiry}
        )

    async def place_order(self, request: OrderRequest) -> OrderResult:
        body = {
            "quantity": request.quantity,
            "product": "D",  # delivery; TODO: make configurable (I=intraday, D=delivery, CO, MTF)
            "validity": "DAY",
            "price": request.price or 0,
            "tag": request.idempotency_key[:20],  # Upstox tags have a length limit — confirm exact limit
            "instrument_token": request.symbol,
            "order_type": _ORDER_TYPE_TO_UPSTOX[request.order_type],
            "transaction_type": _DIRECTION_TO_TRANSACTION_TYPE[request.direction],
            "disclosed_quantity": 0,
            "trigger_price": request.trigger_price or 0,
            "is_amo": False,
        }
        try:
            response = await self._http.post(
                f"{UPSTOX_ORDER_BASE_URL}/order/place",
                headers={**self._headers(), "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            data = _unwrap(response.json())
            return OrderResult(broker_order_id=data.get("order_id", ""), status=OrderStatus.SUBMITTED)
        except httpx.HTTPStatusError as exc:
            reason = _extract_error_message(exc.response)
            return OrderResult(broker_order_id="", status=OrderStatus.REJECTED, rejection_reason=reason)
        except BrokerError as exc:
            # Upstox returns HTTP 200 with a {"status": "error", ...} body
            # for ordinary validation failures (insufficient margin, market
            # closed, bad instrument, ...) -- `_unwrap` above turns that
            # into a raised BrokerError. Callers (ExecutionEngine.submit,
            # then POST /orders and /options/execute) have no try/except
            # around place_order, and by the time this call happens the
            # order is already registered under its idempotency key
            # (OrderManager.create_order), so letting this propagate would
            # 500 the request AND permanently wedge the order at SUBMITTED:
            # any retry with the same order params returns `created=False`
            # and never calls submit() again. A broker-level rejection,
            # whether surfaced as a 4xx/5xx or as a 200-with-error-envelope,
            # must always come back as a normal REJECTED OrderResult so it
            # flows through the existing rejection handling instead.
            return OrderResult(broker_order_id="", status=OrderStatus.REJECTED, rejection_reason=str(exc))

    async def modify_order(self, broker_order_id: str, **changes) -> OrderResult:
        body = {"order_id": broker_order_id, **changes}
        response = await self._http.put(
            f"{UPSTOX_API_BASE_URL}/order/modify", headers={**self._headers(), "Content-Type": "application/json"}, json=body
        )
        response.raise_for_status()
        data = _unwrap(response.json())
        return OrderResult(broker_order_id=data.get("order_id", broker_order_id), status=OrderStatus.ACKNOWLEDGED)

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        response = await self._http.delete(
            f"{UPSTOX_API_BASE_URL}/order/cancel", headers=self._headers(), params={"order_id": broker_order_id}
        )
        response.raise_for_status()
        data = _unwrap(response.json())
        return OrderResult(broker_order_id=data.get("order_id", broker_order_id), status=OrderStatus.CANCELLED)

    async def is_healthy(self) -> bool:
        try:
            await self.get_account()
            return True
        except (httpx.HTTPError, BrokerError):
            return False

    async def aclose(self) -> None:
        await self._http.aclose()


def _extract_error_message(response: httpx.Response) -> str:
    try:
        payload = response.json()
        errors = payload.get("errors") or [{"message": payload.get("message", response.text)}]
        return "; ".join(e.get("message", str(e)) for e in errors)
    except Exception:
        return response.text or f"HTTP {response.status_code}"
