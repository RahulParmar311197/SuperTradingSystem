"""Tests for the Upstox adapter against a mocked HTTP transport — no
network calls, no real credentials. These verify our adapter forms
requests and parses responses the way we *believe* Upstox's v2 API works
based on public docs/search snippets (see the adapter's module docstring);
they cannot substitute for testing against the real API before going live.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.brokers.base import BrokerError, OrderRequest
from app.brokers.upstox.adapter import UpstoxBroker
from app.brokers.upstox.oauth import build_authorization_url, exchange_code_for_token
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType


def _json_response(status_code: int, payload: dict) -> httpx.Response:
    return httpx.Response(status_code, content=json.dumps(payload))


def _broker_with_transport(handler) -> UpstoxBroker:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport)
    return UpstoxBroker(access_token="fake-token", http_client=client)


def test_build_authorization_url_has_required_params():
    url = build_authorization_url("client-1", "https://app.example/cb", state="s1")
    assert "response_type=code" in url
    assert "client_id=client-1" in url
    assert "state=s1" in url
    assert url.startswith("https://api.upstox.com/v2/login/authorization/dialog")


@pytest.mark.asyncio
async def test_exchange_code_for_token_posts_form_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content.decode()
        captured["content_type"] = request.headers["content-type"]
        return _json_response(200, {"access_token": "tok-123"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    result = await exchange_code_for_token("cid", "csecret", "https://cb", "auth-code", http_client=client)

    assert result == {"access_token": "tok-123"}
    assert "code=auth-code" in captured["body"]
    assert "grant_type=authorization_code" in captured["body"]
    assert captured["content_type"] == "application/x-www-form-urlencoded"


@pytest.mark.asyncio
async def test_get_account_parses_funds_and_margin():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/user/get-funds-and-margin"
        assert request.headers["authorization"] == "Bearer fake-token"
        return _json_response(200, {"status": "success", "data": {"equity": {"available_margin": 50000.0, "used_margin": 10000.0}}})

    broker = _broker_with_transport(handler)
    account = await broker.get_account()

    assert account.balance == 50000.0
    assert account.margin_used == 10000.0
    assert account.equity == 60000.0


@pytest.mark.asyncio
async def test_get_positions_parses_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "status": "success",
                "data": [{"instrument_token": "NSE_EQ|INE669E01016", "quantity": 10, "average_price": 100.5, "pnl": 25.0}],
            },
        )

    broker = _broker_with_transport(handler)
    positions = await broker.get_positions()

    assert len(positions) == 1
    assert positions[0].symbol == "NSE_EQ|INE669E01016"
    assert positions[0].quantity == 10
    assert positions[0].unrealized_pnl == 25.0


@pytest.mark.asyncio
async def test_get_orders_maps_status_and_direction():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "status": "success",
                "data": [
                    {
                        "order_id": "o1",
                        "instrument_token": "NSE_EQ|X",
                        "transaction_type": "BUY",
                        "order_type": "MARKET",
                        "quantity": 5,
                        "status": "complete",
                        "filled_quantity": 5,
                        "average_price": 101.0,
                    }
                ],
            },
        )

    broker = _broker_with_transport(handler)
    orders = await broker.get_orders()

    assert orders[0].status == OrderStatus.FILLED
    assert orders[0].direction == Direction.LONG
    assert orders[0].order_type == OrderType.MARKET


@pytest.mark.asyncio
async def test_get_orders_unrecognized_status_falls_back_to_acknowledged():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            200,
            {
                "status": "success",
                "data": [
                    {
                        "order_id": "o1",
                        "instrument_token": "NSE_EQ|X",
                        "transaction_type": "SELL",
                        "order_type": "LIMIT",
                        "quantity": 5,
                        "status": "some-new-status-upstox-invented",
                    }
                ],
            },
        )

    broker = _broker_with_transport(handler)
    orders = await broker.get_orders()
    assert orders[0].status == OrderStatus.ACKNOWLEDGED


@pytest.mark.asyncio
async def test_get_quote_parses_ltp():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["instrument_key"] == "NSE_EQ|X"
        return _json_response(200, {"status": "success", "data": {"NSE_EQ:X": {"last_price": 250.5}}})

    broker = _broker_with_transport(handler)
    quote = await broker.get_quote("NSE_EQ|X")
    assert quote.ltp == 250.5


@pytest.mark.asyncio
async def test_place_order_success_returns_submitted():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/order/place"
        body = json.loads(request.content)
        assert body["transaction_type"] == "BUY"
        assert body["order_type"] == "MARKET"
        return _json_response(200, {"status": "success", "data": {"order_id": "new-order-1"}})

    broker = _broker_with_transport(handler)
    result = await broker.place_order(
        OrderRequest(idempotency_key="k1", symbol="NSE_EQ|X", direction=Direction.LONG, order_type=OrderType.MARKET, quantity=10)
    )

    assert result.status == OrderStatus.SUBMITTED
    assert result.broker_order_id == "new-order-1"


@pytest.mark.asyncio
async def test_place_order_rejection_returns_rejected_result_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(400, {"status": "error", "errors": [{"message": "Insufficient funds"}]})

    broker = _broker_with_transport(handler)
    result = await broker.place_order(
        OrderRequest(idempotency_key="k1", symbol="NSE_EQ|X", direction=Direction.LONG, order_type=OrderType.MARKET, quantity=10)
    )

    assert result.status == OrderStatus.REJECTED
    assert "Insufficient funds" in result.rejection_reason


@pytest.mark.asyncio
async def test_cancel_order():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.params["order_id"] == "o1"
        return _json_response(200, {"status": "success", "data": {"order_id": "o1"}})

    broker = _broker_with_transport(handler)
    result = await broker.cancel_order("o1")
    assert result.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_error_envelope_raises_broker_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"status": "error", "errors": [{"message": "Token expired"}]})

    broker = _broker_with_transport(handler)
    with pytest.raises(BrokerError, match="Token expired"):
        await broker.get_positions()


@pytest.mark.asyncio
async def test_is_healthy_false_on_broker_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(200, {"status": "error", "errors": [{"message": "boom"}]})

    broker = _broker_with_transport(handler)
    assert await broker.is_healthy() is False
