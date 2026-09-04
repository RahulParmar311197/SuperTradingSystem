import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.redis import account_halt_reason, halt_account, resume_account
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserRole, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _cleanup(*user_ids: uuid.UUID) -> None:
    async with async_session_factory() as db:
        for user_id in user_ids:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _register(client: TestClient, label: str) -> tuple[str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    return token, user_id


async def test_admin_endpoints_reject_non_admin_users(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register(client, "notadmin")
        headers = {"Authorization": f"Bearer {token}"}
        try:
            for path in (
                "/admin/users",
                "/admin/broker-connections",
                "/admin/orders",
                "/admin/risk-events",
                "/admin/system-health",
                "/admin/halted-accounts",
            ):
                r = client.get(path, headers=headers)
                assert r.status_code == 403, f"{path}: {r.text}"

            r = client.post("/admin/accounts/some-account/resume", json={"confirm": True}, headers=headers)
            assert r.status_code == 403, r.text
        finally:
            await _cleanup(user_id)


async def test_admin_endpoints_return_data_for_admin_user(require_infra):
    with TestClient(app) as client:
        admin_token, admin_id = await _register(client, "admin")
        other_token, other_id = await _register(client, "regular")

        async with async_session_factory() as db:
            admin_user = await db.get(User, admin_id)
            admin_user.role = UserRole.ADMIN
            await db.commit()

        headers = {"Authorization": f"Bearer {admin_token}"}
        try:
            r = client.get("/admin/users", headers=headers)
            assert r.status_code == 200, r.text
            emails = {u["email"] for u in r.json()}
            assert admin_user.email in emails
            assert len(r.json()) >= 2

            r = client.get("/admin/broker-connections", headers=headers)
            assert r.status_code == 200, r.text
            assert isinstance(r.json(), list)

            r = client.get("/admin/orders", headers=headers)
            assert r.status_code == 200, r.text

            r = client.get("/admin/risk-events", headers=headers)
            assert r.status_code == 200, r.text

            r = client.get("/admin/system-health", headers=headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["total_users"] >= 2
            assert body["database"] == "HEALTHY"
        finally:
            await _cleanup(admin_id, other_id)


async def test_admin_can_view_and_resume_a_halted_account(require_infra):
    with TestClient(app) as client:
        admin_token, admin_id = await _register(client, "admin")

        async with async_session_factory() as db:
            admin_user = await db.get(User, admin_id)
            admin_user.role = UserRole.ADMIN
            await db.commit()

        headers = {"Authorization": f"Bearer {admin_token}"}
        halted_account_id = f"halttest-{uuid.uuid4().hex[:8]}"
        try:
            await halt_account(halted_account_id, "reconciliation mismatch: GHOST position")

            r = client.get("/admin/halted-accounts", headers=headers)
            assert r.status_code == 200, r.text
            halted = {row["account_id"]: row["reason"] for row in r.json()}
            assert halted[halted_account_id] == "reconciliation mismatch: GHOST position"

            # Requires confirm=true.
            r = client.post(f"/admin/accounts/{halted_account_id}/resume", json={"confirm": False}, headers=headers)
            assert r.status_code == 400, r.text
            assert await account_halt_reason(halted_account_id) is not None

            r = client.post(f"/admin/accounts/{halted_account_id}/resume", json={"confirm": True}, headers=headers)
            assert r.status_code == 200, r.text
            assert await account_halt_reason(halted_account_id) is None

            # Resuming an account that isn't halted is a 404, not a silent no-op.
            r = client.post(f"/admin/accounts/{halted_account_id}/resume", json={"confirm": True}, headers=headers)
            assert r.status_code == 404, r.text
        finally:
            await resume_account(halted_account_id)
            await _cleanup(admin_id)
