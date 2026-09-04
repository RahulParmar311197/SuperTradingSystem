import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_new_user_has_no_live_or_auto_trade_permission(require_infra):
    with TestClient(app) as client:
        email = f"perms-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Perms"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
        try:
            r = client.get("/trading-permissions", headers=headers)
            assert r.status_code == 200, r.text
            permissions = r.json()["permissions"]
            assert set(permissions) == {"VIEW", "ANALYZE", "PAPER_TRADE"}
            assert "LIVE_TRADE" not in permissions
            assert "AUTO_TRADE" not in permissions
        finally:
            await _cleanup(user_id)


async def test_grant_requires_confirm(require_infra):
    with TestClient(app) as client:
        email = f"perms-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Perms"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
        try:
            r = client.post("/trading-permissions/grant", json={"permission": "LIVE_TRADE", "confirm": False}, headers=headers)
            assert r.status_code == 400, r.text

            r = client.post("/trading-permissions/grant", json={"permission": "VIEW", "confirm": True}, headers=headers)
            assert r.status_code == 400, r.text  # VIEW is auto-granted, not requestable

            r = client.post("/trading-permissions/grant", json={"permission": "AUTO_TRADE", "confirm": True}, headers=headers)
            assert r.status_code == 200, r.text
            assert "AUTO_TRADE" in r.json()["permissions"]

            r = client.post("/trading-permissions/revoke", json={"permission": "AUTO_TRADE"}, headers=headers)
            assert r.status_code == 200, r.text
            assert "AUTO_TRADE" not in r.json()["permissions"]
        finally:
            await _cleanup(user_id)
