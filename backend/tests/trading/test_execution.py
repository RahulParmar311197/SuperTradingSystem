import pytest

from app.brokers.base import OrderResult
from app.brokers.mock import MockBroker
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.trading.execution import ExecutionEngine
from app.trading.order_manager import IllegalTransitionError, OrderManager
from app.trading.position_manager import PositionManager


@pytest.mark.asyncio
async def test_full_order_lifecycle_updates_position():
    broker = MockBroker()
    broker.set_quote("NIFTY", ltp=25000.0)
    order_manager = OrderManager()
    position_manager = PositionManager()
    engine = ExecutionEngine(broker, order_manager, position_manager)

    order, created = order_manager.create_order(
        idempotency_key="u1-s1-sig1",
        account_id="acct-1",
        symbol="NIFTY",
        direction=Direction.LONG,
        order_type=OrderType.MARKET,
        quantity=10,
    )
    assert created is True

    order_manager.transition(order.id, OrderStatus.VALIDATING)
    order_manager.transition(order.id, OrderStatus.RISK_APPROVED)
    await engine.submit(order.id)

    updated = order_manager.get(order.id)
    assert updated.status == OrderStatus.MONITORING
    assert updated.filled_quantity == 10

    position = position_manager.get("acct-1", "NIFTY")
    assert position.quantity == 10
    assert position.average_price == 25000.0


@pytest.mark.asyncio
async def test_idempotent_order_creation_does_not_duplicate():
    order_manager = OrderManager()
    first, created_first = order_manager.create_order(
        "same-key", "acct-1", "NIFTY", Direction.LONG, OrderType.MARKET, 5
    )
    second, created_second = order_manager.create_order(
        "same-key", "acct-1", "NIFTY", Direction.LONG, OrderType.MARKET, 5
    )
    assert created_first is True
    assert created_second is False
    assert first.id == second.id


def test_illegal_transition_is_rejected():
    order_manager = OrderManager()
    order, _ = order_manager.create_order("k1", "acct-1", "NIFTY", Direction.LONG, OrderType.MARKET, 1)
    with pytest.raises(IllegalTransitionError):
        order_manager.transition(order.id, OrderStatus.FILLED)  # can't skip straight to FILLED


def test_position_manager_realizes_pnl_on_reversal():
    pm = PositionManager()
    pm.apply_fill("acct-1", "NIFTY", Direction.LONG, 10, 100.0)
    position = pm.apply_fill("acct-1", "NIFTY", Direction.SHORT, 10, 110.0)

    assert position.quantity == 0
    assert position.realized_pnl == 100.0  # 10 units * (110-100)


@pytest.mark.asyncio
async def test_a_broker_that_never_answered_leaves_the_order_failed_not_acknowledged():
    # A timeout is not a rejection and it is not an acknowledgement. The
    # order may be filling at the exchange right now, so recording
    # ACKNOWLEDGED (which every branch below the rejection check does)
    # would claim the broker confirmed something it never said, and
    # applying a fill would invent one. FAILED is what
    # ReconciliationWorker exists to resolve against the broker's record.
    class _SilentBroker(MockBroker):
        async def place_order(self, request):
            return OrderResult(
                broker_order_id="",
                status=OrderStatus.FAILED,
                rejection_reason="broker did not answer",
            )

    broker = _SilentBroker()
    broker.set_quote("ACME", ltp=100.0)
    order_manager = OrderManager()
    position_manager = PositionManager()
    engine = ExecutionEngine(broker, order_manager, position_manager)

    order, _ = order_manager.create_order(
        "idem-failed", "acct", "ACME", Direction.LONG, OrderType.MARKET, 10.0, None
    )
    order_manager.transition(order.id, OrderStatus.VALIDATING)
    order_manager.transition(order.id, OrderStatus.RISK_APPROVED)

    await engine.submit(order.id)

    assert order_manager.get(order.id).status == OrderStatus.FAILED
    assert position_manager.get("acct", "ACME") is None, "no fill may be invented for an unanswered order"
