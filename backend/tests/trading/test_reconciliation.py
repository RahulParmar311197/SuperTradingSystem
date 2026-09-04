import uuid

from app.brokers.base import BrokerOrder, BrokerPosition
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.trading.order_manager import OrderRecord
from app.trading.position_manager import PositionRecord
from app.trading.reconciliation import reconcile, reconcile_orders, reconcile_positions


def _local_order(**overrides) -> OrderRecord:
    defaults = dict(
        id=uuid.uuid4(),
        idempotency_key="k1",
        account_id="acct-1",
        symbol="NIFTY",
        direction=Direction.LONG,
        order_type=OrderType.MARKET,
        quantity=10,
        price=None,
        status=OrderStatus.ACKNOWLEDGED,
        broker_order_id="broker-1",
        filled_quantity=0.0,
    )
    defaults.update(overrides)
    return OrderRecord(**defaults)


def test_no_mismatches_when_states_agree():
    local = [_local_order(status=OrderStatus.FILLED, filled_quantity=10)]
    broker = [
        BrokerOrder(
            broker_order_id="broker-1",
            symbol="NIFTY",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            quantity=10,
            price=None,
            status=OrderStatus.FILLED,
            filled_quantity=10,
        )
    ]
    # FILLED isn't in the "open" set the reconciler cares about — closed
    # orders aren't re-checked forever, only ones still live at the broker.
    assert reconcile_orders(local, broker) == []


def test_detects_order_missing_at_broker():
    local = [_local_order(status=OrderStatus.ACKNOWLEDGED, broker_order_id="ghost-order")]
    assert reconcile_orders(local, []) != []


def test_detects_status_mismatch():
    local = [_local_order(status=OrderStatus.ACKNOWLEDGED)]
    broker = [
        BrokerOrder(
            broker_order_id="broker-1",
            symbol="NIFTY",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            quantity=10,
            price=None,
            status=OrderStatus.REJECTED,
        )
    ]
    mismatches = reconcile_orders(local, broker)
    assert any("status mismatch" in m for m in mismatches)


def test_detects_untracked_broker_order():
    broker = [
        BrokerOrder(
            broker_order_id="mystery-order",
            symbol="NIFTY",
            direction=Direction.LONG,
            order_type=OrderType.MARKET,
            quantity=5,
            price=None,
            status=OrderStatus.FILLED,
        )
    ]
    mismatches = reconcile_orders([], broker)
    assert any("unknown locally" in m for m in mismatches)


def test_position_quantity_mismatch_detected():
    local = [PositionRecord(account_id="acct-1", symbol="NIFTY", quantity=10, average_price=100)]
    broker = [BrokerPosition(symbol="NIFTY", quantity=5, average_price=100)]
    mismatches = reconcile_positions(local, broker)
    assert any("quantity mismatch" in m for m in mismatches)


def test_position_missing_at_broker_detected():
    local = [PositionRecord(account_id="acct-1", symbol="NIFTY", quantity=10, average_price=100)]
    mismatches = reconcile_positions(local, [])
    assert any("no position at broker" in m for m in mismatches)


def test_untracked_broker_position_detected():
    broker = [BrokerPosition(symbol="BANKNIFTY", quantity=3, average_price=50000)]
    mismatches = reconcile_positions([], broker)
    assert any("not tracked locally" in m for m in mismatches)


def test_full_reconcile_reports_in_sync_when_everything_matches():
    local_orders = [_local_order(status=OrderStatus.FILLED, filled_quantity=10)]
    local_positions = [PositionRecord(account_id="acct-1", symbol="NIFTY", quantity=10, average_price=100)]
    broker_positions = [BrokerPosition(symbol="NIFTY", quantity=10, average_price=100)]

    report = reconcile(local_orders, [], local_positions, broker_positions)
    assert report.in_sync is True
