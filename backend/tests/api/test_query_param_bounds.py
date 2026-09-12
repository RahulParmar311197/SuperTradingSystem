"""A client-supplied `limit` must never reach Postgres unvalidated.

`limit` was declared `int` with an upper cap on some endpoints and no
bound at all on others, but with no lower bound anywhere. A negative value
was passed straight into `.limit(...)`, which Postgres rejects
(`InvalidRowCountInLimitClauseError`) -- so `?limit=-1` answered **500**,
a server error for what is purely a bad request. Two endpoints also had no
upper bound, letting one request ask for an entire table.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def _register(client: TestClient) -> tuple[dict, uuid.UUID]:
    email = f"bounds-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Bounds"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token

    return {"Authorization": f"Bearer {token}"}, uuid.UUID(decode_token(token, TokenType.ACCESS))


async def _cleanup(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# (path, needs_extra_query) for every listing endpoint taking a `limit`.
_ENDPOINTS = [
    "/notifications",
    "/ai/chat/history",
]


@pytest.mark.parametrize("path", _ENDPOINTS)
async def test_a_negative_limit_is_rejected_not_a_server_error(require_infra, path):
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(f"{path}?limit=-1", headers=headers)
            # The point of the test: 422, not 500. A 500 here means the
            # value reached the database.
            assert r.status_code == 422, r.text
            assert r.json()["detail"][0]["loc"] == ["query", "limit"]
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize("path", _ENDPOINTS)
async def test_an_oversized_limit_is_rejected(require_infra, path):
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(f"{path}?limit=1000000000", headers=headers)
            assert r.status_code == 422, r.text
        finally:
            await _cleanup(user_id)


@pytest.mark.parametrize("path", _ENDPOINTS)
async def test_a_limit_inside_the_bounds_still_works(require_infra, path):
    """Guard against the bounds being set so tightly that ordinary
    requests break."""
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            for limit in (1, 50):
                r = client.get(f"{path}?limit={limit}", headers=headers)
                assert r.status_code == 200, r.text
                assert r.json() == []
        finally:
            await _cleanup(user_id)


async def test_setups_limit_is_bounded_too(require_infra):
    # `GET /markets/setups` had no upper bound at all. It also needs real
    # query params, so it is checked separately: a rejected `limit` must
    # come back 422 before the handler runs.
    with TestClient(app) as client:
        headers, user_id = await _register(client)
        try:
            r = client.get(
                f"/setups?instrument_id={uuid.uuid4()}&timeframe=15m&limit=-1", headers=headers
            )
            assert r.status_code == 422, r.text
            assert any(d["loc"] == ["query", "limit"] for d in r.json()["detail"])
        finally:
            await _cleanup(user_id)
