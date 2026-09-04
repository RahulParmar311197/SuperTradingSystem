"""Dhan broker adapter skeleton (blueprint §51).

This implements the `Broker` interface's shape so the rest of the system
(strategy/risk/execution) can target Dhan without any code changes once
this is filled in — but the actual HTTP calls are intentionally left as
TODOs. Dhan's endpoints, auth headers, and payload formats can change
between API versions; per blueprint §51/§120, always implement against
Dhan's *current* official API documentation rather than a guess baked into
this codebase.

Before enabling live trading through this adapter (blueprint §120):
  1. Implement authentication (`_ensure_authenticated`).
  2. Implement market data / instrument lookup.
  3. Implement quotes.
  4. Implement place/modify/cancel order.
  5. Implement positions/funds retrieval.
  6. Test each of the above against Dhan's sandbox, if available.
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

DHAN_API_BASE_URL = "https://api.dhan.co"  # TODO: confirm against current Dhan docs before use


class DhanBroker(Broker):
    def __init__(self, client_id: str, access_token: str, http_client: httpx.AsyncClient | None = None) -> None:
        self.client_id = client_id
        self.access_token = access_token
        self._http = http_client or httpx.AsyncClient(base_url=DHAN_API_BASE_URL, timeout=10.0)

    def _headers(self) -> dict:
        # TODO: confirm Dhan's current auth header scheme (access-token header name, etc.)
        return {"access-token": self.access_token, "Content-Type": "application/json"}

    async def get_account(self) -> AccountInfo:
        raise NotImplementedError("TODO: implement using Dhan's funds/margin endpoint (see current Dhan API docs)")

    async def get_positions(self) -> list[BrokerPosition]:
        raise NotImplementedError("TODO: implement using Dhan's positions endpoint")

    async def get_orders(self) -> list[BrokerOrder]:
        raise NotImplementedError("TODO: implement using Dhan's order book endpoint")

    async def get_quote(self, symbol: str) -> Quote:
        raise NotImplementedError("TODO: implement using Dhan's market quote / LTP endpoint")

    async def place_order(self, request: OrderRequest) -> OrderResult:
        raise NotImplementedError("TODO: implement using Dhan's place-order endpoint")

    async def modify_order(self, broker_order_id: str, **changes) -> OrderResult:
        raise NotImplementedError("TODO: implement using Dhan's modify-order endpoint")

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise NotImplementedError("TODO: implement using Dhan's cancel-order endpoint")

    async def is_healthy(self) -> bool:
        try:
            await self.get_account()
            return True
        except (NotImplementedError, httpx.HTTPError, BrokerError):
            return False

    async def aclose(self) -> None:
        await self._http.aclose()
