"""A position's `stop` is a price; a protective order is what enforces it.

`PositionRecord.stop` has been recorded for live orders since blueprint
§60 was wired up, but nothing ever acted on it: the paper engine checks a
stop against each candle it is fed, and a live position has no candle loop
and no watcher. Price could run straight through a live stop with the
position left open. These cover the broker-side resting order that now
enforces it, and `MockBroker`'s ability to hold one at all -- before this,
`place_order` sent SL/SL_M through `_resolve_fill_price`, which reads
`request.price` for any non-MARKET type, so an SL_M (market-once-triggered,
carrying no limit price) came back REJECTED and a stop was unplaceable
through the only broker any test or paper account uses.
"""

import uuid

import pytest

from app.brokers.base import OrderRequest
from app.brokers.mock import MockBroker
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.trading.position_manager import PositionRecord
from app.trading.protective_stops import cancel_protective_stop, ensure_protective_stop

pytestmark = pytest.mark.asyncio


def _request(direction: Direction, trigger: float, quantity: float = 10.0, symbol: str = "ACME") -> OrderRequest:
    return OrderRequest(
        idempotency_key=str(uuid.uuid4()),
        symbol=symbol,
        direction=direction,
        order_type=OrderType.SL_M,
        quantity=quantity,
        trigger_price=trigger,
    )


async def _long_position(broker: MockBroker, entry: float = 100.0, quantity: float = 10.0) -> PositionRecord:
    broker.set_quote("ACME", ltp=entry)
    await broker.place_order(
        OrderRequest(
            idempotency_key=str(uuid.uuid4()),
            symbol="ACME",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            quantity=quantity,
        )
    )
    return PositionRecord(
        account_id="acct", symbol="ACME", quantity=quantity, average_price=entry, stop=entry - 5
    )


# --- MockBroker must be able to hold a stop at all -------------------------


async def test_a_stop_order_rests_instead_of_being_rejected():
    broker = MockBroker()
    broker.set_quote("ACME", ltp=100.0)
    result = await broker.place_order(_request(Direction.SHORT, trigger=95.0))
    assert result.status == OrderStatus.ACKNOWLEDGED, result.rejection_reason
    assert result.filled_quantity == 0.0


async def test_a_resting_sell_stop_fires_when_price_falls_through_it():
    broker = MockBroker()
    position = await _long_position(broker)
    await ensure_protective_stop(broker, position)
    assert (await broker.get_positions())[0].quantity == 10.0

    broker.set_quote("ACME", ltp=94.0)

    assert await broker.get_positions() == []


async def test_a_resting_sell_stop_does_not_fire_above_its_trigger():
    broker = MockBroker()
    position = await _long_position(broker)
    await ensure_protective_stop(broker, position)

    broker.set_quote("ACME", ltp=96.0)

    assert (await broker.get_positions())[0].quantity == 10.0


async def test_a_buy_stop_protecting_a_short_fires_when_price_rises_through_it():
    broker = MockBroker()
    broker.set_quote("ACME", ltp=100.0)
    await broker.place_order(
        OrderRequest(
            idempotency_key=str(uuid.uuid4()),
            symbol="ACME",
            direction=Direction.SHORT,
            order_type=OrderType.MARKET,
            quantity=10.0,
        )
    )
    position = PositionRecord(account_id="acct", symbol="ACME", quantity=-10.0, average_price=100.0, stop=105.0)
    await ensure_protective_stop(broker, position)

    broker.set_quote("ACME", ltp=106.0)

    assert await broker.get_positions() == []


async def test_a_gap_through_the_stop_fills_at_the_gapped_price_not_the_trigger():
    # A stop is a trigger, not a guaranteed price. Reporting a fill at the
    # trigger after a gap would overstate every backtested and live result
    # by the size of the gap.
    broker = MockBroker()
    position = await _long_position(broker)
    await ensure_protective_stop(broker, position)

    broker.set_quote("ACME", ltp=80.0)

    stop_orders = [o for o in await broker.get_orders() if o.order_type == OrderType.SL_M]
    assert len(stop_orders) == 1
    assert stop_orders[0].status == OrderStatus.FILLED
    assert stop_orders[0].average_fill_price == 80.0


async def test_a_stop_already_through_its_trigger_fires_immediately():
    # A sell-stop placed above the market is already triggered, and a real
    # broker fires it at once. Pinned because it is the visible
    # consequence of an inverted bracket (a long whose stop sits above its
    # entry): the position closes straight away rather than resting behind
    # protection that can never help it.
    broker = MockBroker()
    position = await _long_position(broker)
    position.stop = 105.0

    await ensure_protective_stop(broker, position)

    assert await broker.get_positions() == []


async def test_cancelling_a_resting_stop_stops_it_firing():
    broker = MockBroker()
    position = await _long_position(broker)
    await ensure_protective_stop(broker, position)

    await cancel_protective_stop(broker, position)
    broker.set_quote("ACME", ltp=80.0)

    assert (await broker.get_positions())[0].quantity == 10.0
    assert position.protective_order_id is None


async def test_cancelling_an_already_filled_stop_does_not_unfill_it():
    broker = MockBroker()
    position = await _long_position(broker)
    order_id = await ensure_protective_stop(broker, position)
    broker.set_quote("ACME", ltp=80.0)

    result = await broker.cancel_order(order_id)

    assert result.status == OrderStatus.FILLED
    assert await broker.get_positions() == []


# --- keeping the resting order in step with the position -------------------


async def test_the_protective_order_is_replaced_when_the_position_grows():
    broker = MockBroker()
    position = await _long_position(broker)
    first = await ensure_protective_stop(broker, position)

    position.quantity = 25.0
    second = await ensure_protective_stop(broker, position)

    assert second is not None and second != first
    live = [o for o in await broker.get_orders() if o.order_type == OrderType.SL_M and o.status == OrderStatus.ACKNOWLEDGED]
    assert len(live) == 1, "a stale stop covering only part of the position must not be left resting"
    assert live[0].quantity == 25.0


async def test_closing_the_position_leaves_no_stop_resting():
    # A stop left resting against a closed position is not merely useless:
    # when it fires it opens a fresh naked position the other way.
    broker = MockBroker()
    position = await _long_position(broker)
    await ensure_protective_stop(broker, position)

    # Close it for real, the way a reducing order would.
    await broker.place_order(
        OrderRequest(
            idempotency_key=str(uuid.uuid4()),
            symbol="ACME",
            direction=Direction.SHORT,
            order_type=OrderType.MARKET,
            quantity=10.0,
        )
    )
    position.quantity = 0.0
    assert await ensure_protective_stop(broker, position) is None
    assert position.protective_order_id is None

    # Price runs through where the stop used to be. Nothing may open.
    broker.set_quote("ACME", ltp=80.0)
    assert await broker.get_positions() == []
    assert [
        o for o in await broker.get_orders()
        if o.order_type == OrderType.SL_M and o.status == OrderStatus.ACKNOWLEDGED
    ] == []


async def test_a_position_with_no_stop_gets_no_protective_order():
    broker = MockBroker()
    position = await _long_position(broker)
    position.stop = None

    assert await ensure_protective_stop(broker, position) is None
    assert [o for o in await broker.get_orders() if o.order_type == OrderType.SL_M] == []
