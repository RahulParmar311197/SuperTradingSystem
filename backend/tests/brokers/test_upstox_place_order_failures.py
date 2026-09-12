"""`Broker.place_order` must never raise, whatever the network does.

Its contract (app/brokers/base.py) says so explicitly, and for a good
reason: `ExecutionEngine.submit` has no try/except around the call, and by
the time it runs the order is already registered under its idempotency
key. An exception therefore both 500s the request and wedges the order
forever -- a retry with the same params returns `created=False` and never
calls `submit()` again.

`UpstoxBroker` honoured that for an HTTP 4xx/5xx and for Upstox's
200-with-error-envelope, but not for a transport failure: a connect
timeout, a read timeout, a dropped connection, or a proxy answering with
something that is not JSON all escaped. No broker double anywhere in this
suite raised from `place_order`, which is why nothing caught it.
"""

import uuid

import httpx
import pytest

from app.brokers.base import OrderRequest
from app.brokers.upstox.adapter import UpstoxBroker
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType

pytestmark = pytest.mark.asyncio


def _request() -> OrderRequest:
    return OrderRequest(
        idempotency_key=str(uuid.uuid4()),
        symbol="NSE_EQ|INE002A01018",
        direction=Direction.LONG,
        order_type=OrderType.MARKET,
        quantity=10.0,
    )


def _broker(handler) -> UpstoxBroker:
    return UpstoxBroker(access_token="token", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("connect timed out"),
        httpx.ReadTimeout("read timed out"),
        httpx.ConnectError("connection refused"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
)
async def test_a_transport_failure_comes_back_as_failed_not_an_exception(error):
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    result = await _broker(handler).place_order(_request())

    assert result.status == OrderStatus.FAILED
    assert "unknown" in (result.rejection_reason or "").lower()


async def test_an_unreadable_response_body_comes_back_as_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    result = await _broker(handler).place_order(_request())

    assert result.status == OrderStatus.FAILED


async def test_a_timeout_is_not_reported_as_a_rejection():
    # The distinction is the whole point: REJECTED asserts no order reached
    # the market and the account is flat. A timeout may have placed a real
    # order that is filling right now, and saying "rejected" would tell the
    # user and the risk engine's exposure math that they are flat when they
    # may not be.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timed out")

    result = await _broker(handler).place_order(_request())

    assert result.status != OrderStatus.REJECTED


async def test_a_broker_rejection_is_still_a_rejection():
    # The unambiguous case must keep its precise meaning: Upstox answering
    # 200 with an error envelope really does mean no order was placed.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "error", "errors": [{"message": "Insufficient funds"}]})

    result = await _broker(handler).place_order(_request())

    assert result.status == OrderStatus.REJECTED
    assert "Insufficient funds" in (result.rejection_reason or "")
