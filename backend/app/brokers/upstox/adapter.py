"""Upstox broker adapter skeleton (blueprint §52).

Same caveat as `app.brokers.dhan.adapter`: Upstox's OAuth flow, endpoints,
and payload shapes can change between API versions, so implement each
method against Upstox's *current* official API documentation (blueprint
§52/§120) rather than trusting anything hardcoded here.

Rollout checklist before live trading (blueprint §120):
  1. Implement OAuth authentication (`_ensure_authenticated`).
  2. Implement market data / instrument lookup.
  3. Implement quotes and the option chain + Greeks endpoints.
  4. Implement place/modify/cancel order.
  5. Implement positions retrieval.
  6. Test each of the above against Upstox's sandbox, if available.
  7. Test disconnect/reconnect handling.
  8. Only then flip this account out of paper mode.
"""

from __future__ import annotations

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

UPSTOX_API_BASE_URL = "https://api.upstox.com"  # TODO: confirm against current Upstox docs (versioned path) before use


class UpstoxBroker(Broker):
    def __init__(self, client_id: str, access_token: str, http_client: httpx.AsyncClient | None = None) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self._http = http_client or httpx.AsyncClient(base_url=UPSTOX_API_BASE_URL, timeout=10.0)

    def _headers(self) -> dict:
        # TODO: confirm Upstox's current bearer-token header scheme
        return {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}

    async def get_account(self) -> AccountInfo:
        raise NotImplementedError("TODO: implement using Upstox's funds & margin endpoint")

    async def get_positions(self) -> list[BrokerPosition]:
        raise NotImplementedError("TODO: implement using Upstox's positions endpoint")

    async def get_orders(self) -> list[BrokerOrder]:
        raise NotImplementedError("TODO: implement using Upstox's order book endpoint")

    async def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError("TODO: implement using Upstox's market quote endpoint")

    async def get_option_chain(self, underlying: str, expiry: str) -> dict:
        raise NotImplementedError("TODO: implement using Upstox's option chain endpoint (includes Greeks)")

    async def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError("TODO: implement using Upstox's place-order endpoint")

    async def modify_order(self, broker_order_id: str, **changes) -> OrderResult:
        raise NotImplementedError("TODO: implement using Upstox's modify-order endpoint")

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError("TODO: implement using Upstox's cancel-order endpoint")

    async def is_healthy(self) -> bool:
        try:
            await self.get_account()
            return True
        except (NotImplementedError, httpx.HTTPError, BrokerError):
            return False

    async def aclose(self) -> None:
        await self._http.aclose()
