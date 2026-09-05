import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.notifications import Notification, NotificationType
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register(client: TestClient, label: str) -> tuple[str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    return token, user_id


async def _cleanup(user_ids: list[uuid.UUID]) -> None:
    async with async_session_factory() as db:
        for user_id in user_ids:
            await db.execute(delete(Notification).where(Notification.user_id == user_id))
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_list_and_mark_notifications_read(require_infra):
    # Regression test for the `notifications` table (blueprint §63/§104):
    # create_notification() is a real, tested writer called from
    # reconciliation_worker.py/auto_trade_worker.py, but no endpoint
    # anywhere ever read the rows back -- a write-only table.
    with TestClient(app) as client:
        token, user_id = await _register(client, "notifyowner")
        headers = {"Authorization": f"Bearer {token}"}

        async with async_session_factory() as db:
            n1 = Notification(user_id=user_id, type=NotificationType.TRADE_EXECUTED, title="Trade filled", body="Bought 10 NIFTY")
            n2 = Notification(user_id=user_id, type=NotificationType.SL_HIT, title="Stop hit", body="Position closed at stop")
            db.add(n1)
            db.add(n2)
            await db.commit()
            await db.refresh(n1)
            await db.refresh(n2)

        try:
            r = client.get("/notifications", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert len(body) == 2
            assert all(n["read"] is False for n in body)

            r = client.get("/notifications", params={"unread_only": True}, headers=headers)
            assert len(r.json()) == 2

            r = client.patch(f"/notifications/{n1.id}/read", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["read"] is True

            r = client.get("/notifications", params={"unread_only": True}, headers=headers)
            unread = r.json()
            assert len(unread) == 1
            assert unread[0]["id"] == str(n2.id)
        finally:
            await _cleanup([user_id])


async def test_user_cannot_mark_another_users_notification_read(require_infra):
    with TestClient(app) as client:
        owner_token, owner_id = await _register(client, "notifyowner2")
        other_token, other_id = await _register(client, "notifyother")

        async with async_session_factory() as db:
            n = Notification(user_id=owner_id, type=NotificationType.TRADE_EXECUTED, title="Trade filled", body="Bought 10 NIFTY")
            db.add(n)
            await db.commit()
            await db.refresh(n)

        try:
            other_headers = {"Authorization": f"Bearer {other_token}"}
            r = client.get("/notifications", headers=other_headers)
            assert r.status_code == 200, r.text
            assert r.json() == []

            r = client.patch(f"/notifications/{n.id}/read", headers=other_headers)
            assert r.status_code == 404, r.text

            owner_headers = {"Authorization": f"Bearer {owner_token}"}
            r = client.get("/notifications", headers=owner_headers)
            assert len(r.json()) == 1
            assert r.json()[0]["read"] is False
        finally:
            await _cleanup([owner_id, other_id])
