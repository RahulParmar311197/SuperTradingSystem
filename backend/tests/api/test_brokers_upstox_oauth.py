"""Integration tests for the Upstox OAuth connect flow (app/api/brokers.py),
against real Postgres/Redis but a mocked Upstox token endpoint."""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.users import BrokerAccount, User, UserSession
from app.database.session import async_session_factory
from app.main import app


@pytest.fixture
def registered_user(require_infra):
    with TestClient(app) as client:
        email = f"upstox-oauth-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Upstox OAuth Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        assert r.status_code == 200, r.text
        token = r.json()["access_token"]
        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
        yield client, token, user_id


@pytest.fixture(autouse=True)
def _configure_upstox_settings(monkeypatch):
    from app.core.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("UPSTOX_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("UPSTOX_SECRET", "test-secret")
    yield
    get_settings.cache_clear()


async def _cleanup_user(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(BrokerAccount).where(BrokerAccount.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def test_authorize_returns_a_login_url_and_requires_auth(registered_user):
    client, token, user_id = registered_user
    try:
        r = client.get("/brokers/upstox/authorize", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert "authorization_url" in r.json()
        assert "login/authorization/dialog" in r.json()["authorization_url"]

        anon = client.get("/brokers/upstox/authorize")
        assert anon.status_code == 401
    finally:
        import asyncio

        asyncio.run(_cleanup_user(user_id))


def test_callback_rejects_unknown_state(registered_user):
    client, token, user_id = registered_user
    try:
        r = client.get("/brokers/upstox/callback", params={"code": "somecode", "state": "never-issued"})
        assert r.status_code == 400
    finally:
        import asyncio

        asyncio.run(_cleanup_user(user_id))


def test_full_oauth_round_trip_creates_broker_account(registered_user, monkeypatch):
    client, token, user_id = registered_user
    try:
        r = client.get("/brokers/upstox/authorize", headers={"Authorization": f"Bearer {token}"})
        auth_url = r.json()["authorization_url"]
        state = auth_url.split("state=")[1].split("&")[0]

        async def fake_exchange(client_id, client_secret, redirect_uri, code, http_client=None):
            assert client_id == "test-client-id"
            assert code == "the-auth-code"
            return {"access_token": "fake-access-token-123", "user_id": "UPX123"}

        monkeypatch.setattr("app.api.brokers.exchange_code_for_token", fake_exchange)

        r = client.get("/brokers/upstox/callback", params={"code": "the-auth-code", "state": state})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["broker"] == "UPSTOX"
        assert body["status"] == "ACTIVE"

        # replaying the same state must fail — it was consumed
        r2 = client.get("/brokers/upstox/callback", params={"code": "the-auth-code", "state": state})
        assert r2.status_code == 400

        listed = client.get("/brokers", headers={"Authorization": f"Bearer {token}"})
        assert any(b["broker"] == "UPSTOX" for b in listed.json())
    finally:
        import asyncio

        asyncio.run(_cleanup_user(user_id))
