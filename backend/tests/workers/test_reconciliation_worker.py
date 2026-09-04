import uuid

from sqlalchemy import delete

from app.auth.security import hash_password
from app.brokers.mock import MockBroker
from app.core.redis import account_halt_reason, resume_account
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog
from app.database.models.strategy import Direction
from app.database.models.users import User
from app.database.session import async_session_factory
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager
from app.workers.reconciliation_worker import ReconciliationWorker


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
