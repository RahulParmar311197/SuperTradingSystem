import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

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


async def _register_and_login(client: TestClient, label: str) -> tuple[str, str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    user_id = uuid.UUID(r.json()["id"])
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    assert r.status_code == 200, r.text
    body = r.json()
    return body["access_token"], body["refresh_token"], user_id


async def test_logout_revokes_the_session_and_refresh_no_longer_works(require_infra):
    # Regression test: before this, UserSession.revoked was only ever set
    # as a side effect of /auth/refresh rotating a used token -- no user
    # action could revoke a session at all. A stolen refresh token or a
    # forgotten logged-in shared computer stayed valid until its multi-day
    # natural expiry with no self-service remediation. This proves logout
    # actually closes that: after logout, the same refresh token can never
    # mint a new access token again.
    with TestClient(app) as client:
        _, refresh_token, user_id = await _register_and_login(client, "logouttest")
        try:
            r = client.post("/auth/logout", json={"refresh_token": refresh_token})
            assert r.status_code == 204, r.text

            r = client.post("/auth/refresh", json={"refresh_token": refresh_token})
            assert r.status_code == 401, r.text
        finally:
            await _cleanup(user_id)


async def test_logout_is_idempotent_for_an_already_revoked_or_bogus_token(require_infra):
    # Logout must never be harder to reach than the thing it undoes --
    # logging out twice, or with a token that never mapped to a real
    # session, are both "already logged out" from the caller's point of
    # view, not errors.
    with TestClient(app) as client:
        _, refresh_token, user_id = await _register_and_login(client, "logoutidempotent")
        try:
            r = client.post("/auth/logout", json={"refresh_token": refresh_token})
            assert r.status_code == 204, r.text

            r = client.post("/auth/logout", json={"refresh_token": refresh_token})
            assert r.status_code == 204, r.text

            r = client.post("/auth/logout", json={"refresh_token": "not-a-real-jwt-at-all"})
            assert r.status_code == 204, r.text
        finally:
            await _cleanup(user_id)


async def test_sessions_lists_device_info_and_revoke_removes_it(require_infra):
    # Regression test: UserSession.device_info has been written at every
    # login since the beginning, but nothing ever read it back --
    # write-only data, making "device tracking" (blueprint §69)
    # nonfunctional. This proves GET /auth/sessions actually surfaces it,
    # and that POST /auth/sessions/{id}/revoke removes a session from that
    # list (via the account-holder's own action, distinct from /logout's
    # "revoke the session I'm currently using").
    with TestClient(app) as client:
        email = f"sessionslist-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Sessions Test"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])

        r = client.post(
            "/auth/login",
            json={"email": email, "password": "testpass123"},
            headers={"User-Agent": "pytest-device-a"},
        )
        access_token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {access_token}"}

        try:
            r = client.get("/auth/sessions", headers=headers)
            assert r.status_code == 200, r.text
            sessions = r.json()
            assert len(sessions) == 1
            session_id = sessions[0]["id"]

            r = client.post(f"/auth/sessions/{session_id}/revoke", headers=headers)
            assert r.status_code == 204, r.text

            r = client.get("/auth/sessions", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json() == []
        finally:
            await _cleanup(user_id)


async def test_user_cannot_revoke_another_users_session(require_infra):
    with TestClient(app) as client:
        _, _, owner_id = await _register_and_login(client, "sessionowner")
        other_token, _, other_id = await _register_and_login(client, "sessionother")

        try:
            async with async_session_factory() as db:
                owner_session = (
                    await db.execute(select(UserSession).where(UserSession.user_id == owner_id))
                ).scalars().first()
                owner_session_id = owner_session.id

            r = client.post(
                f"/auth/sessions/{owner_session_id}/revoke",
                headers={"Authorization": f"Bearer {other_token}"},
            )
            assert r.status_code == 404, r.text

            async with async_session_factory() as db:
                refreshed = (await db.execute(select(UserSession).where(UserSession.id == owner_session_id))).scalar_one()
                assert refreshed.revoked is False
        finally:
            await _cleanup(owner_id)
            await _cleanup(other_id)
