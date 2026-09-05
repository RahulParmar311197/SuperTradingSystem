import uuid

import pytest
from sqlalchemy import delete

from app.brokers.base import BrokerError
from app.brokers.dhan.adapter import DhanBroker
from app.brokers.mock import MockBroker
from app.brokers.upstox.adapter import UpstoxBroker
from app.core.encryption import encrypt_credentials
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User
from app.database.session import async_session_factory
from app.trading.broker_resolver import resolve_broker

pytestmark = pytest.mark.asyncio


async def _make_user(db) -> User:
    user = User(
        email=f"resolver-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="x",
        name="Resolver Test",
        trading_permissions=[],
    )
    db.add(user)
    await db.flush()
    return user


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(BrokerAccount).where(BrokerAccount.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_no_connected_account_resolves_to_mock_broker(require_infra):
    async with async_session_factory() as db:
        user = await _make_user(db)
        await db.commit()
        try:
            broker, broker_account_id = await resolve_broker(db, user)
            assert isinstance(broker, MockBroker)
            # No connected account -- nothing to attribute a paper order's
            # Order.broker_account_id to.
            assert broker_account_id is None
        finally:
            await _cleanup(user.id)


async def test_active_upstox_account_resolves_to_a_real_upstox_broker(require_infra):
    async with async_session_factory() as db:
        user = await _make_user(db)
        account = BrokerAccount(
            user_id=user.id,
            broker=BrokerName.UPSTOX,
            encrypted_credentials=encrypt_credentials({"access_token": "test-token-123"}),
            status=BrokerAccountStatus.ACTIVE,
        )
        db.add(account)
        await db.commit()
        try:
            broker, broker_account_id = await resolve_broker(db, user)
            assert isinstance(broker, UpstoxBroker)
            assert broker.access_token == "test-token-123"
            # Regression test: resolve_broker used to discard `account.id`
            # once the adapter was built, so nothing could ever populate
            # Order.broker_account_id -- a real FK column that existed
            # purely to trace a placed order back to the connected account
            # that executed it, permanently NULL for every order ever
            # placed.
            assert broker_account_id == account.id
        finally:
            await _cleanup(user.id)


async def test_active_dhan_account_resolves_to_a_dhan_broker(require_infra):
    async with async_session_factory() as db:
        user = await _make_user(db)
        account = BrokerAccount(
            user_id=user.id,
            broker=BrokerName.DHAN,
            encrypted_credentials=encrypt_credentials({"client_id": "cid-1", "access_token": "tok-1"}),
            status=BrokerAccountStatus.ACTIVE,
        )
        db.add(account)
        await db.commit()
        try:
            broker, broker_account_id = await resolve_broker(db, user)
            assert isinstance(broker, DhanBroker)
            assert broker.client_id == "cid-1"
            assert broker.access_token == "tok-1"
            assert broker_account_id == account.id
        finally:
            await _cleanup(user.id)


async def test_disconnected_account_is_ignored(require_infra):
    async with async_session_factory() as db:
        user = await _make_user(db)
        db.add(
            BrokerAccount(
                user_id=user.id,
                broker=BrokerName.UPSTOX,
                encrypted_credentials=encrypt_credentials({"access_token": "stale-token"}),
                status=BrokerAccountStatus.DISCONNECTED,
            )
        )
        await db.commit()
        try:
            broker, broker_account_id = await resolve_broker(db, user)
            assert isinstance(broker, MockBroker)
            assert broker_account_id is None
        finally:
            await _cleanup(user.id)


async def test_missing_access_token_raises_broker_error(require_infra):
    async with async_session_factory() as db:
        user = await _make_user(db)
        db.add(
            BrokerAccount(
                user_id=user.id,
                broker=BrokerName.UPSTOX,
                encrypted_credentials=encrypt_credentials({"refresh_token": "no-access-token-here"}),
                status=BrokerAccountStatus.ACTIVE,
            )
        )
        await db.commit()
        try:
            with pytest.raises(BrokerError):
                await resolve_broker(db, user)
        finally:
            await _cleanup(user.id)
