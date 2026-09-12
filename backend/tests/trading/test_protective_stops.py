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

from app.brokers.base import BrokerError, OrderRequest, OrderResult
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
    order_id = (await ensure_protective_stop(broker, position)).order_id
    broker.set_quote("ACME", ltp=80.0)

    result = await broker.cancel_order(order_id)

    assert result.status == OrderStatus.FILLED
    assert await broker.get_positions() == []


# --- keeping the resting order in step with the position -------------------


async def test_the_protective_order_is_replaced_when_the_position_grows():
    broker = MockBroker()
    position = await _long_position(broker)
    first = (await ensure_protective_stop(broker, position)).order_id

    position.quantity = 25.0
    second = (await ensure_protective_stop(broker, position)).order_id

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
    result = await ensure_protective_stop(broker, position)
    # No stop was wanted -- which must not read as "a stop was refused".
    assert result.order_id is None and not result.is_unprotected
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

    result = await ensure_protective_stop(broker, position)
    assert result.order_id is None and not result.is_unprotected
    assert [o for o in await broker.get_orders() if o.order_type == OrderType.SL_M] == []


# --- reporting a stop the broker would not take ----------------------------
#
# `ensure_protective_stop` used to return `str | None`, spelling "no stop
# was wanted" and "a stop was wanted and refused" the same way -- and its
# only caller discarded the value, so a refused stop was an ERROR in a log
# and nothing else.


class _NoStopOrdersBroker(MockBroker):
    """A broker that will not take stop orders. Routine in real life: a
    trigger too close to the last price, a freeze quantity, or stop orders
    simply not accepted for this segment right now."""

    def __init__(self, outcome: OrderStatus = OrderStatus.REJECTED) -> None:
        super().__init__()
        self._outcome = outcome

    async def place_order(self, request):
        if request.order_type in (OrderType.SL, OrderType.SL_M):
            return OrderResult(broker_order_id="", status=self._outcome, rejection_reason="Stop orders not accepted")
        return await super().place_order(request)


async def test_a_refused_stop_is_reported_as_unprotected_not_as_no_stop_wanted():
    broker = _NoStopOrdersBroker()
    position = await _long_position(broker)

    result = await ensure_protective_stop(broker, position)

    assert result.is_unprotected
    assert result.order_id is None
    assert not result.fate_unknown
    assert "Stop orders not accepted" in result.problem
    assert position.protective_order_id is None


async def test_an_unanswered_stop_is_flagged_fate_unknown():
    # A broker that never answered may or may not have a stop resting.
    # That is not the same as knowing there is none, and a caller must be
    # able to tell the two apart.
    broker = _NoStopOrdersBroker(OrderStatus.FAILED)
    position = await _long_position(broker)

    result = await ensure_protective_stop(broker, position)

    assert result.is_unprotected
    assert result.fate_unknown


async def test_a_placed_stop_reports_no_problem():
    broker = MockBroker()
    position = await _long_position(broker)

    result = await ensure_protective_stop(broker, position)

    assert not result.is_unprotected
    assert result.order_id == position.protective_order_id


# --- a cancel whose outcome is unknown -------------------------------------
#
# `cancel_protective_stop` used to clear `position.protective_order_id`
# *before* attempting the cancel and swallow every exception. A cancel that
# never landed -- a timeout, a 5xx, a dropped connection -- therefore lost
# the only reference to an order that was still resting at the venue, and
# `ensure_protective_stop` went straight on to place a second stop on top
# of it, reporting `problem=None`.
#
# Measured on the code before this change: a 200-unit long ended up with two
# resting SL_M orders, for 100 and 200 units, totalling 300. Both fire
# together when price reaches the stop: 300 sold against a 200 position
# leaves the account short 100 with nothing guarding it -- the naked
# position this module exists to prevent, created by the machinery meant to
# prevent it.


class _CancelFailsBroker(MockBroker):
    """A broker whose cancel never lands. The resting order stays exactly
    where it was, which is the whole point: the failure is in the request,
    not at the venue."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cancel_attempts = 0

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        self.cancel_attempts += 1
        raise BrokerError("upstream timeout -- the venue never answered")


class _AlreadyCompleteBroker(MockBroker):
    """The *ordinary* cancel failure: the stop already fired, so the venue
    refuses to cancel it. Upstox surfaces this as the same `BrokerError` a
    timeout produces, which is why the fix asks `get_orders` rather than
    reading the message."""

    async def cancel_order(self, broker_order_id: str) -> OrderResult:
        raise BrokerError("Order already complete")


def _resting_stops(broker: MockBroker) -> list:
    return [
        order
        for order in broker._orders.values()
        if order.order_type == OrderType.SL_M
        and order.status not in (OrderStatus.CANCELLED, OrderStatus.FILLED, OrderStatus.REJECTED)
    ]


async def test_a_cancel_that_never_landed_does_not_get_a_second_stop_on_top():
    """Behavioural proof, and the one that matters: no over-sell.

    The assertion is on the total quantity resting on stops, not on the
    order count, because that is the quantity that actually hits the market
    when price reaches the trigger.
    """
    broker = _CancelFailsBroker()
    position = await _long_position(broker, entry=1000.0, quantity=100.0)
    position.stop = 980.0

    first = await ensure_protective_stop(broker, position)
    assert first.order_id is not None, "fixture: the first stop must rest"
    assert len(_resting_stops(broker)) == 1

    # A second fill: the position doubles and the stop moves up.
    position.quantity = 200.0
    position.stop = 985.0
    second = await ensure_protective_stop(broker, position)

    assert broker.cancel_attempts == 1, "the cancel must still be attempted"
    resting = _resting_stops(broker)
    assert sum(order.quantity for order in resting) <= position.quantity, (
        f"{sum(o.quantity for o in resting)} units rest on stops against a "
        f"{position.quantity}-unit position: they fire together and over-sell it into a "
        f"naked position facing the other way ({[o.quantity for o in resting]})"
    )
    assert second.is_unprotected, "a position whose stop could not be replaced must be reported"
    assert second.fate_unknown is True, "nobody knows whether the old stop is live; say so"
    assert position.protective_order_id == first.order_id, (
        "the id of an order that may still be resting is the only handle anyone has on it "
        "and must not be discarded"
    )


async def test_the_ordinary_cancel_failure_is_still_ordinary():
    """Control, and the regression this fix could most easily cause.

    A stop that has already fired makes the venue refuse the cancel with
    the same exception type a timeout raises. That is the routine end of
    every stopped-out trade, and it must not report a problem, must not
    keep a stale id, and must not stand in the way of the next entry --
    otherwise doing its job would halt the account every time.
    """
    broker = _AlreadyCompleteBroker()
    position = await _long_position(broker, entry=1000.0, quantity=100.0)
    position.stop = 980.0
    first = await ensure_protective_stop(broker, position)
    assert first.order_id is not None

    # The stop fires at the venue; the position closes.
    broker._orders[first.order_id].status = OrderStatus.FILLED
    position.quantity = 0.0
    closed = await ensure_protective_stop(broker, position)
    assert closed.problem is None, f"a fired stop is not a problem: {closed.problem}"
    assert position.protective_order_id is None, "a settled order's id must be released"

    # And the next entry gets its own stop as usual.
    position.quantity = 50.0
    position.stop = 990.0
    reentry = await ensure_protective_stop(broker, position)
    assert reentry.problem is None, reentry.problem
    assert reentry.order_id is not None


async def test_a_broker_that_cannot_be_asked_is_treated_as_unknown():
    """Control on the fallback: when `get_orders` fails too, nothing is
    known about the old stop, and unknown must not be read as gone."""

    class _BlindBroker(_CancelFailsBroker):
        async def get_orders(self):
            raise BrokerError("orderbook unavailable")

    broker = _BlindBroker()
    position = await _long_position(broker, entry=1000.0, quantity=100.0)
    position.stop = 980.0
    first = await ensure_protective_stop(broker, position)

    position.quantity = 200.0
    second = await ensure_protective_stop(broker, position)
    assert second.is_unprotected and second.fate_unknown is True
    assert position.protective_order_id == first.order_id
    assert len(_resting_stops(broker)) == 1, "no second stop was placed on an unknown outcome"


async def test_a_stop_the_broker_has_forgotten_is_treated_as_gone():
    """Control on the other side of the same fallback. A venue that does
    not list an order at all is not about to fire it, so this must behave
    exactly like a clean cancel -- otherwise the fix would wedge every
    position whose broker prunes its orderbook."""

    class _ForgetfulBroker(_CancelFailsBroker):
        async def get_orders(self):
            return []

    broker = _ForgetfulBroker()
    position = await _long_position(broker, entry=1000.0, quantity=100.0)
    position.stop = 980.0
    await ensure_protective_stop(broker, position)

    position.quantity = 200.0
    position.stop = 985.0
    second = await ensure_protective_stop(broker, position)
    assert second.problem is None, second.problem
    assert second.order_id is not None
    assert position.protective_order_id == second.order_id
