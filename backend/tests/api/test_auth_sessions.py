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


async def test_refresh_token_reuse_revokes_every_session_and_audits(require_infra):
    # Regression test: `refresh()` correctly rotates tokens (revokes the
    # used session, issues a fresh pair), but presenting an
    # already-rotated-out refresh token a second time -- the textbook
    # signal that a token was stolen and raced against the legitimate
    # client -- used to be treated as an ordinary invalid-token error with
    # no side effect at all: the session the rotation produced stayed
    # live, and nothing was ever written anywhere (no AuditLog row), so a
    # real compromise would go completely unnoticed and unremediated.
    with TestClient(app) as client:
        _, refresh_token_a, user_id = await _register_and_login(client, "refreshreuse")
        try:
            r = client.post("/auth/refresh", json={"refresh_token": refresh_token_a})
            assert r.status_code == 200, r.text
            refresh_token_b = r.json()["refresh_token"]

            # Reusing the now-rotated-out token_a is the attack signature.
            r = client.post("/auth/refresh", json={"refresh_token": refresh_token_a})
            assert r.status_code == 401, r.text

            # Containment: token_b (the session the legitimate rotation
            # produced, with no link back to token_a at all) must also be
            # dead now, not just token_a. Reusing it too is *itself* a
            # second reuse of an already-revoked token -- caught by the
            # same detection, hence two audit rows below, not one.
            r = client.post("/auth/refresh", json={"refresh_token": refresh_token_b})
            assert r.status_code == 401, r.text

            async with async_session_factory() as db:
                sessions = (await db.execute(select(UserSession).where(UserSession.user_id == user_id))).scalars().all()
                assert len(sessions) >= 2
                assert all(s.revoked for s in sessions)

                audits = (
                    await db.execute(
                        select(AuditLog).where(
                            AuditLog.user_id == user_id, AuditLog.action == "auth.refresh_token_reuse_detected"
                        )
                    )
                ).scalars().all()
                assert len(audits) == 2
        finally:
            await _cleanup(user_id)


async def test_sessions_lists_device_info_and_revoke_removes_it(require_infra):
    # This test set a User-Agent header and named itself after
    # device_info, but asserted only on len(sessions) and the session id --
    # so it passed for years while every row's device_info was NULL,
    # because POST /auth/login never passed one to _issue_tokens at all.
    # The assertion its name always implied is now here; see also
    # test_login_records_the_calling_device below. It still proves the
    # other half: POST /auth/sessions/{id}/revoke removes a session from
    # the list (the account-holder's own action, distinct from /logout's
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
            assert sessions[0]["device_info"] == "pytest-device-a"
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


async def test_login_records_the_calling_device_so_sessions_are_distinguishable(require_infra):
    # Regression test: `_issue_tokens` has accepted `device_info` since the
    # beginning and `refresh()` carries it across rotations, but POST
    # /auth/login never supplied one -- it took no `Request` and called the
    # service with three positional arguments, so the parameter fell back
    # to its `None` default on every login of every user. GET
    # /auth/sessions therefore listed N indistinguishable rows, and
    # "which of these is the attacker's session?" -- the question
    # POST /auth/sessions/{id}/revoke exists to answer -- was unanswerable.
    desktop = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome/120.0.0.0 Safari/537.36"
    phone = "SuperTradingSystem-Android/1.4 (Pixel 8)"
    with TestClient(app) as client:
        email = f"devicetrack-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Device"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])
        try:
            r = client.post(
                "/auth/login",
                json={"email": email, "password": "testpass123"},
                headers={"User-Agent": desktop},
            )
            assert r.status_code == 200, r.text
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

            r = client.post(
                "/auth/login",
                json={"email": email, "password": "testpass123"},
                headers={"User-Agent": phone},
            )
            assert r.status_code == 200, r.text

            r = client.get("/auth/sessions", headers=headers)
            assert r.status_code == 200, r.text
            devices = sorted(s["device_info"] for s in r.json())
            assert devices == sorted([desktop, phone]), devices
        finally:
            await _cleanup(user_id)


async def test_an_over_long_user_agent_is_truncated_rather_than_failing_the_login(require_infra):
    # `device_info` is String(500) and a User-Agent is unbounded client
    # input. Postgres raises StringDataRightTruncation rather than
    # silently truncating (measured: 500 chars inserts, 501 does not), so
    # an unguarded header would turn someone's login into a 500.
    long_agent = "Mozilla/5.0 " + "X" * 900
    with TestClient(app) as client:
        email = f"devicelong-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Long"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])
        try:
            r = client.post(
                "/auth/login",
                json={"email": email, "password": "testpass123"},
                headers={"User-Agent": long_agent},
            )
            assert r.status_code == 200, r.text
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

            r = client.get("/auth/sessions", headers=headers)
            assert r.status_code == 200, r.text
            stored = r.json()[0]["device_info"]
            assert len(stored) == 500
            assert stored == long_agent[:500]
        finally:
            await _cleanup(user_id)


async def test_a_login_with_no_user_agent_stores_no_device(require_infra):
    # A bare API client sends no User-Agent; that is not an error, it just
    # leaves the column NULL as it always was.
    with TestClient(app) as client:
        email = f"devicenone-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "None"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])
        try:
            r = client.post(
                "/auth/login",
                json={"email": email, "password": "testpass123"},
                headers={"User-Agent": ""},
            )
            assert r.status_code == 200, r.text
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

            r = client.get("/auth/sessions", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()[0]["device_info"] is None
        finally:
            await _cleanup(user_id)


async def test_the_device_survives_a_refresh_token_rotation(require_infra):
    # `refresh()` re-issues with `device_info=session.device_info`, which
    # was only ever propagating None. Now that a real value exists, a
    # rotated session must keep naming the same device -- otherwise a
    # long-lived session would lose its label the first time its token
    # rotated.
    agent = "SuperTradingSystem-iOS/2.0 (iPhone 15)"
    with TestClient(app) as client:
        email = f"devicerotate-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Rotate"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])
        try:
            r = client.post(
                "/auth/login",
                json={"email": email, "password": "testpass123"},
                headers={"User-Agent": agent},
            )
            assert r.status_code == 200, r.text
            refresh_token = r.json()["refresh_token"]

            r = client.post("/auth/refresh", json={"refresh_token": refresh_token})
            assert r.status_code == 200, r.text
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

            r = client.get("/auth/sessions", headers=headers)
            assert r.status_code == 200, r.text
            assert [s["device_info"] for s in r.json()] == [agent]
        finally:
            await _cleanup(user_id)
