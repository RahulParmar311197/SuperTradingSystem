import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.api.orders import all_stacks
from app.brokers.base import BrokerPosition
from app.core.redis import account_halt_reason, resume_account
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog
from app.database.models.users import BrokerAccount, User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.trading.live_reconciliation import reconcile_all_connected_accounts

pytestmark = pytest.mark.asyncio


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(BrokerAccount).where(BrokerAccount.user_id == user_id))
        await db.execute(delete(Notification).where(Notification.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()
    await resume_account(str(user_id))


async def test_reconciliation_halts_account_on_broker_position_mismatch(require_infra):
    with TestClient(app) as client:
        email = f"livereconcile-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Reconcile Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        try:
            r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": True}, headers=headers)
            assert r.status_code == 200, r.text

            # Triggers _stack_for -> resolve_broker: no BrokerAccount exists
            # yet, so this user's stack is backed by MockBroker.
            r = client.get("/orders", headers=headers)
            assert r.status_code == 200, r.text

            # Connect a broker account *after* the stack already exists —
            # reconciliation only needs the DB row to know this account
            # should be checked; it reconciles against whatever broker the
            # already-created stack holds (MockBroker here).
            r = client.post(
                "/brokers/connect", json={"broker": "UPSTOX", "credentials": {"access_token": "irrelevant-for-this-test"}}, headers=headers
            )
            assert r.status_code == 201, r.text

            stack = all_stacks()[user_id]
            assert await account_halt_reason(str(user_id)) is None

            # Inject a broker-side position this stack's local
            # PositionManager has never heard of -> a real mismatch.
            stack.broker._positions["GHOSTSYM"] = BrokerPosition(symbol="GHOSTSYM", quantity=10, average_price=100.0)

            checked = await reconcile_all_connected_accounts()
            assert checked >= 1

            reason = await account_halt_reason(str(user_id))
            assert reason is not None
            assert "GHOSTSYM" in reason
        finally:
            await _cleanup(user_id)


async def test_reconciliation_skips_accounts_with_no_live_stack_yet(require_infra):
    """A user who connected a broker but never placed an order this
    process lifetime has no entry in `all_stacks()` — reconciliation must
    skip them, not crash."""
    async with async_session_factory() as db:
        user = User(email=f"nostack-{uuid.uuid4().hex[:8]}@example.com", password_hash="x", name="No Stack", trading_permissions=[])
        db.add(user)
        await db.flush()
        from app.core.encryption import encrypt_credentials
        from app.database.models.users import BrokerAccountStatus, BrokerName

        db.add(
            BrokerAccount(
                user_id=user.id,
                broker=BrokerName.UPSTOX,
                encrypted_credentials=encrypt_credentials({"access_token": "x"}),
                status=BrokerAccountStatus.ACTIVE,
            )
        )
        await db.commit()
        user_id = user.id

    try:
        assert user_id not in all_stacks()
        await reconcile_all_connected_accounts()  # must not raise
        assert await account_halt_reason(str(user_id)) is None
    finally:
        await _cleanup(user_id)
