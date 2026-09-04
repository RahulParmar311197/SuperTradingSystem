import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio


async def test_compute_greeks_returns_values_without_crashing(require_infra):
    # Regression test: POST /options/greeks previously 500'd on every call
    # (`Greeks` is `@dataclass(slots=True)`, and `greeks.__dict__` raises
    # AttributeError on it) because no test had ever actually exercised
    # this endpoint before -- the same bug class already fixed at five
    # other call sites this project (replay, ai explain-trade, backtest
    # run, markets candles), just missed here.
    with TestClient(app) as client:
        email = f"greeks-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Greeks Test"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        try:
            r = client.post(
                "/options/greeks",
                json={"spot": 25000.0, "strike": 25000.0, "time_to_expiry_years": 0.0833, "iv": 0.15, "option_type": "CALL"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            for field in ("price", "delta", "gamma", "theta", "vega", "rho"):
                assert field in body
            assert body["price"] > 0
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
                await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
