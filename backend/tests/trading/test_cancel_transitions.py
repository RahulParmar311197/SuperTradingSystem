"""The cancel endpoint's guard and the transition table are one contract.

`POST /orders/{id}/cancel` decides which statuses may be cancelled;
`app.trading.order_manager._ALLOWED_TRANSITIONS` decides which may actually
move to CANCELLED. They disagreed: the endpoint admitted SUBMITTED, the table
did not, so every cancel of a SUBMITTED order raised `IllegalTransitionError`
-- converted to a 500 by the catch-all handler in `app/main.py` -- leaving
the persist, audit and websocket-publish work below the transition unrun and
the order's status unmoved.

That state is also the one most in need of cancelling. `ExecutionEngine.submit`
transitions to SUBMITTED and *then* awaits `broker.place_order`; if that call
raises, the order rests there permanently. `UpstoxBroker.place_order` converts
`HTTPStatusError` and `BrokerError` into a REJECTED result but lets transport
failures propagate, and such an order has no `broker_order_id` and no `orders`
row at all, so `reconcile_orders` reports it as "SUBMITTED locally but never
submitted to the broker" and halts the account on every pass.

Why the existing tests missed it:
`tests/api/test_orders.py::test_cancel_order_broker_failure_is_surfaced_cleanly_and_leaves_status_unchanged`
is the only test of this endpoint, and its docstring claims to cover "a still
cancelable order" -- but its `_NeverFillsBroker` *returns* an ACKNOWLEDGED
result rather than raising, and `ExecutionEngine.submit` unconditionally
transitions to ACKNOWLEDGED anyway, which the table always permitted. No test
double in the suite raises from `place_order`, so no fixture could produce an
order resting at SUBMITTED (shape b), and the SUBMITTED row of the table had
no test of its own at all.
"""

import pytest

from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.trading.order_manager import _ALLOWED_TRANSITIONS, IllegalTransitionError, OrderManager

# The statuses `app/api/orders.py::cancel_order` admits past its 409 guard.
CANCELLABLE_STATUSES = (OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED)

_PATH_TO = {
    OrderStatus.SUBMITTED: (OrderStatus.VALIDATING, OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTED),
    OrderStatus.ACKNOWLEDGED: (
        OrderStatus.VALIDATING,
        OrderStatus.RISK_APPROVED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACKNOWLEDGED,
    ),
}


def _order_resting_at(status: OrderStatus) -> tuple[OrderManager, object]:
    manager = OrderManager()
    order, _ = manager.create_order(
        f"key-{status.value}", "acct", "TESTSYM", Direction.LONG, OrderType.MARKET, 10
    )
    for step in _PATH_TO[status]:
        manager.transition(order.id, step)
    assert manager.get(order.id).status is status
    return manager, order


@pytest.mark.parametrize("status", CANCELLABLE_STATUSES, ids=lambda s: s.value)
def test_every_status_the_endpoint_admits_can_reach_cancelled(status):
    # The structural assertion, pinning the relationship rather than one
    # instance: if the endpoint's guard or the table is ever widened without
    # the other, this fails. Pre-fix the SUBMITTED case raised
    # IllegalTransitionError -> 500.
    assert OrderStatus.CANCELLED in _ALLOWED_TRANSITIONS[status], (
        f"cancel_order admits {status.value} but the transition table cannot move it to CANCELLED"
    )


@pytest.mark.parametrize("status", CANCELLABLE_STATUSES, ids=lambda s: s.value)
def test_cancelling_from_each_admitted_status_actually_works(status):
    # And the behavioural half: the transition really runs and lands.
    manager, order = _order_resting_at(status)

    manager.transition(order.id, OrderStatus.CANCELLED, "cancelled by user")

    assert manager.get(order.id).status is OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_a_broker_failure_leaves_the_order_resting_at_submitted():
    # The state this fix exists for. `ExecutionEngine.submit` moves to
    # SUBMITTED before awaiting the broker, so a raising `place_order` leaves
    # it there with no broker_order_id -- and nothing else can move it, since
    # a retry on the same idempotency key returns created=False and never
    # calls submit() again. No other test double in the suite raises here,
    # which is why no fixture could reach this status.
    from app.trading.execution import ExecutionEngine
    from app.trading.position_manager import PositionManager

    class _TransportFailureBroker:
        async def place_order(self, request):
            raise ConnectionError("connection reset by peer")

    manager = OrderManager()
    engine = ExecutionEngine(_TransportFailureBroker(), manager, PositionManager())
    order, _ = manager.create_order("wedged", "acct", "TESTSYM", Direction.LONG, OrderType.MARKET, 10)
    manager.transition(order.id, OrderStatus.VALIDATING)
    manager.transition(order.id, OrderStatus.RISK_APPROVED)

    with pytest.raises(ConnectionError):
        await engine.submit(order.id)

    resting = manager.get(order.id)
    assert resting.status is OrderStatus.SUBMITTED
    assert resting.broker_order_id is None

    # The escape hatch has to work on exactly this order.
    manager.transition(order.id, OrderStatus.CANCELLED, "cancelled by user")
    assert manager.get(order.id).status is OrderStatus.CANCELLED


def test_a_terminal_order_still_cannot_be_cancelled():
    # The control: widening SUBMITTED must not have made CANCELLED reachable
    # from everywhere. A filled-and-closed order is done.
    manager = OrderManager()
    order, _ = manager.create_order("done", "acct", "TESTSYM", Direction.LONG, OrderType.MARKET, 10)
    for step in (
        OrderStatus.VALIDATING,
        OrderStatus.RISK_APPROVED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.FILLED,
        OrderStatus.CLOSED,
    ):
        manager.transition(order.id, step)

    with pytest.raises(IllegalTransitionError):
        manager.transition(order.id, OrderStatus.CANCELLED)


def test_the_rest_of_the_submitted_row_is_unchanged():
    # The other control: this adds one edge, it does not rewrite the row.
    assert _ALLOWED_TRANSITIONS[OrderStatus.SUBMITTED] == {
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.FAILED,
    }
