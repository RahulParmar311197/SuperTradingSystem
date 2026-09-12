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


# --- the resting protective stop --------------------------------------------
#
# `app/trading/protective_stops.py` places a position's broker-side stop by
# calling `broker.place_order` directly, never through `OrderManager`, so it
# has no `OrderRecord` and no entry in the local broker-id index. A resting
# stop reports ACKNOWLEDGED, which is in the set `reconcile_orders` treats as
# "a live order nobody knows about".
#
# So every live entry carrying a stop was flagged, and `ReconciliationWorker`
# halts the account and raises RECONCILIATION_REQUIRED on any mismatch --
# within 60 seconds, every time, with resuming a deliberate manual admin
# action. Two features that are each correct alone made live trading
# unusable between them. Measured end to end before the fix: one MARKET entry
# plus the stop it triggers produced
# `Broker order <id> for ACME is unknown locally`, `in_sync=False`.


def _protective_stop(broker_order_id: str = "stop-1", *, status=OrderStatus.ACKNOWLEDGED) -> BrokerOrder:
    return BrokerOrder(
        broker_order_id=broker_order_id,
        symbol="NIFTY",
        direction=Direction.SHORT,
        order_type=OrderType.SL_M,
        quantity=10,
        price=None,
        status=status,
    )


def _guarded_position(protective_order_id: str | None = "stop-1") -> PositionRecord:
    return PositionRecord(
        account_id="acct-1",
        symbol="NIFTY",
        quantity=10,
        average_price=100,
        stop=95,
        protective_order_id=protective_order_id,
    )


def test_a_positions_own_protective_stop_is_not_an_unknown_order():
    """Behavioural proof. The position holds the only reference to this
    order, and that reference is what makes it known."""
    report = reconcile(
        [],
        [_protective_stop()],
        [_guarded_position()],
        [BrokerPosition(symbol="NIFTY", quantity=10, average_price=100)],
    )
    assert report.in_sync is True, (
        f"a position's own protective stop was reported as a mismatch, which halts the "
        f"account: {report.order_mismatches}"
    )


def test_a_stop_no_position_claims_is_still_unknown():
    """Control, and the one that keeps the exemption narrow. The same
    resting SL_M, with no position referencing it, is exactly the orphan
    reconciliation exists to catch."""
    report = reconcile(
        [],
        [_protective_stop()],
        [_guarded_position(protective_order_id=None)],
        [BrokerPosition(symbol="NIFTY", quantity=10, average_price=100)],
    )
    assert any("unknown locally" in m for m in report.order_mismatches), (
        f"an unclaimed resting stop must still be flagged: {report.order_mismatches}"
    )


def test_another_brokers_order_is_still_unknown_even_next_to_a_stop():
    """Control: the exemption covers the exact ids the positions name and
    nothing else. An order placed outside this system -- by hand at the
    broker's own terminal, say -- is still a mismatch."""
    report = reconcile(
        [],
        [_protective_stop(), _protective_stop("someone-elses-order")],
        [_guarded_position()],
        [BrokerPosition(symbol="NIFTY", quantity=10, average_price=100)],
    )
    assert len(report.order_mismatches) == 1, report.order_mismatches
    assert "someone-elses-order" in report.order_mismatches[0]


def test_a_fired_stop_is_still_caught_by_the_position_check():
    """Control on what the exemption must not hide.

    When the stop fires, the order goes FILLED and the position closes at
    the broker while local state still shows it open. Exempting the order
    is safe precisely because the consequence shows up on the other side of
    the report -- and if it ever stopped doing so, this fails.
    """
    report = reconcile(
        [],
        [_protective_stop(status=OrderStatus.FILLED)],
        [_guarded_position()],
        [],
    )
    assert report.in_sync is False
    assert any("no position at broker" in m for m in report.position_mismatches), (
        f"a fired stop must still surface as a position mismatch: {report.position_mismatches}"
    )


def test_a_flat_position_still_shields_the_stop_it_has_not_released():
    """Control on the other edge. `reconcile` collects the ids from every
    local position, not only the open ones: a position that has just gone
    flat still holds the id until the cancel is confirmed, and flagging it
    in that window would halt the account for a stop that is being retired
    normally."""
    flat = PositionRecord(
        account_id="acct-1", symbol="NIFTY", quantity=0, average_price=100, protective_order_id="stop-1"
    )
    report = reconcile([], [_protective_stop()], [flat], [])
    assert report.in_sync is True, report.order_mismatches
