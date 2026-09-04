import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


@pytest.fixture
def user_without_auto_trade_permission(require_infra):
    with TestClient(app) as client:
        email = f"noauto-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "No Auto"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
        yield client, token, user_id


@pytest.fixture
def user_with_auto_trade_permission(require_infra):
    import asyncio

    with TestClient(app) as client:
        email = f"withauto-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "With Auto"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        async def grant():
            async with async_session_factory() as db:
                user = await db.get(User, user_id)
                user.trading_permissions = ["AUTO_TRADE"]
                await db.commit()

        asyncio.run(grant())
        yield client, token, user_id


def test_status_defaults_to_disabled(user_without_auto_trade_permission):
    client, token, user_id = user_without_auto_trade_permission
    try:
        r = client.get("/auto-trading/status", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["enabled"] is False
    finally:
        import asyncio

        asyncio.run(_cleanup(user_id))


def test_enable_requires_auto_trade_permission(user_without_auto_trade_permission):
    client, token, user_id = user_without_auto_trade_permission
    try:
        r = client.post("/auto-trading/enable", json={"confirm": True}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 403
    finally:
        import asyncio

        asyncio.run(_cleanup(user_id))


def test_enable_requires_explicit_confirm(user_with_auto_trade_permission):
    client, token, user_id = user_with_auto_trade_permission
    try:
        r = client.post("/auto-trading/enable", json={"confirm": False}, headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 400

        r = client.get("/auto-trading/status", headers={"Authorization": f"Bearer {token}"})
        assert r.json()["enabled"] is False
    finally:
        import asyncio

        asyncio.run(_cleanup(user_id))


def test_enable_then_disable_round_trip(user_with_auto_trade_permission):
    client, token, user_id = user_with_auto_trade_permission
    try:
        r = client.post(
            "/auto-trading/enable",
            json={"confirm": True, "risk_per_trade_pct": 0.25, "max_positions": 2},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["enabled"] is True
        assert body["risk_per_trade_pct"] == 0.25
        assert body["max_positions"] == 2

        r = client.post("/auto-trading/disable", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["enabled"] is False
    finally:
        import asyncio

        asyncio.run(_cleanup(user_id))
