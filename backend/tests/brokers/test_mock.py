import pytest

from app.brokers.base import OrderRequest
from app.brokers.mock import MockBroker
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType

pytestmark = pytest.mark.asyncio


def _request(order_type: OrderType, price: float | None) -> OrderRequest:
    return OrderRequest(
        idempotency_key=f"k-{order_type.value}-{price}",
        symbol="TESTSYM",
        direction=Direction.LONG,
        order_type=order_type,
        quantity=10.0,
        price=price,
    )


@pytest.mark.parametrize("order_type", [OrderType.LIMIT, OrderType.SL, OrderType.SL_M])
async def test_an_order_with_no_usable_price_is_rejected_not_filled_at_zero(order_type):
    # Regression test: `_resolve_fill_price` read `request.price or 0.0`
    # for any non-MARKET order, so an order carrying no price filled at
    # **0.0** -- a position at `average_price=0.0`, reported as zero
    # exposure, whose eventual close books the whole notional as
    # fabricated profit. A fill at zero is not a cheap fill.
    #
    # `MockBroker._resolve_fill_price` had no direct test at all: the only
    # fill-price assertion in the suite pinned `order_type=MARKET`, and no
    # API test ever sent an `order_type` or `price` field, so every order
    # the suite placed took the MARKET branch.
    broker = MockBroker()
    result = await broker.place_order(_request(order_type, price=None))

    assert result.status == OrderStatus.REJECTED
    assert result.average_fill_price is None
    assert "No usable price" in (result.rejection_reason or "")
    # A rejected order must leave no position behind.
    assert await broker.get_positions() == []


@pytest.mark.parametrize("price", [0.0, -5.0])
async def test_a_non_positive_price_is_also_refused(price):
    broker = MockBroker()
    result = await broker.place_order(_request(OrderType.LIMIT, price=price))
    assert result.status == OrderStatus.REJECTED
    assert await broker.get_positions() == []


async def test_a_limit_order_fills_at_its_own_price_not_at_the_quote():
    # The quote is deliberately different from the limit price, so this
    # cannot pass by reading the wrong one.
    broker = MockBroker()
    broker.set_quote("TESTSYM", ltp=250.0)
    result = await broker.place_order(_request(OrderType.LIMIT, price=100.0))

    assert result.status == OrderStatus.FILLED
    assert result.average_fill_price == pytest.approx(100.0)


async def test_a_market_order_still_fills_from_the_quote():
    broker = MockBroker()
    broker.set_quote("TESTSYM", ltp=250.0)
    result = await broker.place_order(_request(OrderType.MARKET, price=None))

    assert result.status == OrderStatus.FILLED
    assert result.average_fill_price == pytest.approx(250.0)


async def test_a_market_order_with_no_quote_anywhere_is_rejected():
    # Previously this resolved to 0.0 as well -- the MARKET branch falls
    # back to `request.price`, and with neither a quote nor a price there
    # is nothing to fill at.
    broker = MockBroker()
    result = await broker.place_order(_request(OrderType.MARKET, price=None))
    assert result.status == OrderStatus.REJECTED
    assert await broker.get_positions() == []
