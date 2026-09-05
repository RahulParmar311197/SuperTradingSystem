import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

import app.auth.service as auth_service
from app.auth.security import DUMMY_PASSWORD_HASH
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


async def test_login_with_unknown_email_still_pays_bcrypt_cost(require_infra, monkeypatch):
    # Regression test: `login()` used to short-circuit on `user is None`
    # via `or`, skipping `verify_password` (bcrypt.checkpw, deliberately
    # slow) entirely for an email with no account -- while a real email
    # always paid that cost. That's a timing side channel: an
    # unauthenticated caller could tell "email registered" from "email not
    # registered" purely from response latency, independent of the
    # password guess. The fix always calls verify_password, using a
    # precomputed dummy hash when no account matches.
    calls: list[tuple[str, str]] = []
    real_verify_password = auth_service.verify_password

    def _spy(password: str, password_hash: str) -> bool:
        calls.append((password, password_hash))
        return real_verify_password(password, password_hash)

    monkeypatch.setattr(auth_service, "verify_password", _spy)

    with TestClient(app) as client:
        r = client.post(
            "/auth/login", json={"email": f"nosuchuser-{uuid.uuid4().hex[:8]}@example.com", "password": "irrelevant123"}
        )
        assert r.status_code == 401, r.text

    assert len(calls) == 1
    assert calls[0][1] == DUMMY_PASSWORD_HASH


async def test_login_with_wrong_password_for_real_user_fails(require_infra):
    with TestClient(app) as client:
        email = f"loginwrongpw-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "correctpass123", "name": "Login Test"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])

        try:
            r = client.post("/auth/login", json={"email": email, "password": "wrongpass123"})
            assert r.status_code == 401, r.text
        finally:
            await _cleanup(user_id)


async def test_login_with_correct_password_succeeds(require_infra):
    with TestClient(app) as client:
        email = f"loginok-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "correctpass123", "name": "Login Test"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])

        try:
            r = client.post("/auth/login", json={"email": email, "password": "correctpass123"})
            assert r.status_code == 200, r.text
            assert r.json()["access_token"]
        finally:
            await _cleanup(user_id)
