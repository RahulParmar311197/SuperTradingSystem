import uuid

from sqlalchemy import delete, select

from app.auth.security import hash_password
from app.brokers.base import BrokerError
from app.brokers.mock import MockBroker
from app.core.redis import account_halt_reason, resume_account
from app.database.models.notifications import Notification, NotificationType
from app.database.models.risk import AuditLog
from app.database.models.strategy import Direction
from app.database.models.users import User
from app.database.session import async_session_factory
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager
from app.workers.reconciliation_worker import ReconciliationWorker


class _UnreachableBroker(MockBroker):
    """Simulates a real adapter's behavior when the broker can't be
    reached at all (expired token, network failure, outage) -- unlike
    `MockBroker.set_healthy(False)`, which only changes what `is_healthy()`
    reports, a real disconnect makes `get_orders`/`get_positions`
    themselves raise (see `UpstoxBroker._get`'s `response.raise_for_status()`)."""

    async def get_orders(self):
        raise BrokerError("simulated broker outage")


async def test_reconciliation_passes_when_states_agree(require_infra):
    account_id = f"acct-{uuid.uuid4().hex[:8]}"
    broker = MockBroker()
    order_manager = OrderManager()
    position_manager = PositionManager()

    worker = ReconciliationWorker(account_id, uuid.uuid4(), broker, order_manager, position_manager)
    report = await worker.run_once()

    assert report.in_sync is True
    assert await account_halt_reason(account_id) is None


async def test_reconciliation_halts_account_on_mismatch(require_infra):
    account_id = f"acct-{uuid.uuid4().hex[:8]}"
    broker = MockBroker()
    order_manager = OrderManager()
    position_manager = PositionManager()

    # Local state believes a position is open; the (mock) broker has never
    # heard of it — exactly the kind of drift a disconnect can cause.
    position_manager.apply_fill(account_id, "NIFTY", Direction.LONG, 10, 25000)

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"reconcile-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Reconciliation Test",
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    try:
        worker = ReconciliationWorker(account_id, user_id, broker, order_manager, position_manager)
        report = await worker.run_once()

        assert report.in_sync is False
        halt_reason = await account_halt_reason(account_id)
        assert halt_reason is not None
        assert "NIFTY" in halt_reason
    finally:
        await resume_account(account_id)
        async with async_session_factory() as db:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(Notification).where(Notification.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_reconciliation_does_not_repeat_the_mismatch_alert_on_every_pass(require_infra):
    # Regression test: `run_once` unconditionally wrote a fresh AuditLog
    # row and Notification on *every* pass a mismatch was detected, with
    # no check for whether the account was already halted for this same,
    # still-unresolved incident. Resuming is a deliberate manual admin
    # action (this worker's own docstring), and `live_reconciliation.run()`
    # calls this every 60 seconds indefinitely -- so a single mismatch
    # that isn't immediately resolved used to flood GET /notifications and
    # the audit trail with a duplicate row every minute for as long as it
    # went unnoticed.
    account_id = f"acct-{uuid.uuid4().hex[:8]}"
    broker = MockBroker()
    order_manager = OrderManager()
    position_manager = PositionManager()
    position_manager.apply_fill(account_id, "NIFTY", Direction.LONG, 10, 25000)

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"reconcile-repeat-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Reconciliation Repeat Test",
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    try:
        worker = ReconciliationWorker(account_id, user_id, broker, order_manager, position_manager)
        await worker.run_once()
        # A second pass over the exact same, still-unresolved mismatch --
        # no `resume_account` call in between.
        report = await worker.run_once()
        assert report.in_sync is False

        async with async_session_factory() as db:
            notifications = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            audits = (await db.execute(select(AuditLog).where(AuditLog.user_id == user_id))).scalars().all()
        assert len(notifications) == 1
        assert len(audits) == 1
    finally:
        await resume_account(account_id)
        async with async_session_factory() as db:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(Notification).where(Notification.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_reconciliation_does_not_repeat_the_broker_unreachable_alert_on_every_pass(require_infra):
    # Same fix, other branch: a sustained broker outage keeps raising on
    # every pass for as long as it lasts, and must alert once per
    # incident, not once per pass.
    account_id = f"acct-{uuid.uuid4().hex[:8]}"
    broker = _UnreachableBroker()
    order_manager = OrderManager()
    position_manager = PositionManager()

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"reconcile-unreachable-repeat-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Reconciliation Unreachable Repeat Test",
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    try:
        worker = ReconciliationWorker(account_id, user_id, broker, order_manager, position_manager)
        await worker.run_once()
        # A second pass over the exact same, still-unresolved outage.
        report = await worker.run_once()
        assert report.in_sync is False

        async with async_session_factory() as db:
            notifications = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalars().all()
            audits = (await db.execute(select(AuditLog).where(AuditLog.user_id == user_id))).scalars().all()
        assert len(notifications) == 1
        assert len(audits) == 1
    finally:
        await resume_account(account_id)
        async with async_session_factory() as db:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(Notification).where(Notification.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()


async def test_reconciliation_halts_account_when_broker_is_unreachable(require_infra):
    # Regression test: `run_once` called `self.broker.get_orders()` with no
    # try/except. A real adapter raises rather than returning stale data
    # when the broker is actually unreachable (expired token, network
    # failure, outage) -- exactly blueprint §74's "Broker Failure Handling"
    # scenario, as opposed to the mismatch case above. That exception used
    # to propagate out of `run_once` and get swallowed by a bare
    # `except Exception: logger.exception(...)` in
    # app/trading/live_reconciliation.py -- no halt, no notification,
    # `BrokerAccount.status` stuck at ACTIVE forever, and
    # `NotificationType.BROKER_DISCONNECTED` a dead enum value nothing ever
    # emitted.
    account_id = f"acct-{uuid.uuid4().hex[:8]}"
    broker = _UnreachableBroker()
    order_manager = OrderManager()
    position_manager = PositionManager()

    async with async_session_factory() as db:
        user = User(
            id=uuid.uuid4(),
            email=f"reconcile-unreachable-{uuid.uuid4().hex[:8]}@example.com",
            password_hash=hash_password("irrelevant123"),
            name="Reconciliation Unreachable Test",
        )
        db.add(user)
        await db.commit()
        user_id = user.id

    try:
        worker = ReconciliationWorker(account_id, user_id, broker, order_manager, position_manager)
        report = await worker.run_once()

        assert report.in_sync is False
        halt_reason = await account_halt_reason(account_id)
        assert halt_reason is not None
        assert "unreachable" in halt_reason.lower()

        async with async_session_factory() as db:
            notification = (
                await db.execute(select(Notification).where(Notification.user_id == user_id))
            ).scalar_one()
            assert notification.type == NotificationType.BROKER_DISCONNECTED

            audit = (await db.execute(select(AuditLog).where(AuditLog.user_id == user_id))).scalar_one()
            assert audit.action == "reconciliation.broker_unreachable"
    finally:
        await resume_account(account_id)
        async with async_session_factory() as db:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(Notification).where(Notification.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
            await db.commit()
