import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
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


async def _cleanup(user_ids: list[uuid.UUID], strategy_ids: list[uuid.UUID]) -> None:
    async with async_session_factory() as db:
        for strategy_id in strategy_ids:
            await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.id.in_(strategy_ids)))
        for user_id in user_ids:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
            await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


def _strategy_payload(name: str, minimum_rr: float = 2.0) -> dict:
    return {
        "name": name,
        "market": "NIFTY",
        "timeframe": "15m",
        "direction": "bullish",
        "conditions": [{"type": "fvg", "direction": "bullish"}],
        "entry": {"type": "fvg_retest"},
        "risk": {"risk_percent": 1.0, "minimum_rr": minimum_rr},
    }


async def test_creating_a_strategy_snapshots_version_1(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register(client, "stratver")
        headers = {"Authorization": f"Bearer {token}"}
        strategy_id = None
        try:
            r = client.post("/strategies", json=_strategy_payload("V1 strategy"), headers=headers)
            assert r.status_code == 201, r.text
            strategy_id = r.json()["id"]

            r = client.get(f"/strategies/{strategy_id}/versions", headers=headers)
            assert r.status_code == 200, r.text
            versions = r.json()
            assert len(versions) == 1
            assert versions[0]["version"] == 1
            assert versions[0]["definition"]["risk"]["minimum_rr"] == 2.0
        finally:
            await _cleanup([user_id], [uuid.UUID(strategy_id)] if strategy_id else [])


async def test_updating_a_strategy_preserves_every_prior_version(require_infra):
    # Regression test for the false claim that PUT /strategies/{id} "bumps
    # version rather than overwriting history" -- it used to overwrite the
    # same row's `definition` in place with no history table at all, so
    # once a strategy was edited, the definition an earlier trade's
    # `strategy_version` pointed to was unrecoverable.
    with TestClient(app) as client:
        token, user_id = await _register(client, "stratver2")
        headers = {"Authorization": f"Bearer {token}"}
        strategy_id = None
        try:
            r = client.post("/strategies", json=_strategy_payload("Original", minimum_rr=2.0), headers=headers)
            strategy_id = r.json()["id"]

            r = client.put(f"/strategies/{strategy_id}", json=_strategy_payload("Edited once", minimum_rr=3.0), headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["version"] == 2

            r = client.put(f"/strategies/{strategy_id}", json=_strategy_payload("Edited twice", minimum_rr=4.0), headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["version"] == 3

            r = client.get(f"/strategies/{strategy_id}/versions/1", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["name"] == "Original"
            assert r.json()["definition"]["risk"]["minimum_rr"] == 2.0

            r = client.get(f"/strategies/{strategy_id}/versions/2", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["name"] == "Edited once"
            assert r.json()["definition"]["risk"]["minimum_rr"] == 3.0

            r = client.get(f"/strategies/{strategy_id}/versions/3", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["name"] == "Edited twice"
            assert r.json()["definition"]["risk"]["minimum_rr"] == 4.0

            r = client.get(f"/strategies/{strategy_id}/versions", headers=headers)
            assert [v["version"] for v in r.json()] == [1, 2, 3]

            r = client.get(f"/strategies/{strategy_id}/versions/4", headers=headers)
            assert r.status_code == 404, r.text
        finally:
            await _cleanup([user_id], [uuid.UUID(strategy_id)] if strategy_id else [])


async def test_user_cannot_access_another_users_strategy_versions(require_infra):
    with TestClient(app) as client:
        owner_token, owner_id = await _register(client, "stratverowner")
        other_token, other_id = await _register(client, "stratverother")
        strategy_id = None
        try:
            r = client.post(
                "/strategies", json=_strategy_payload("Private"), headers={"Authorization": f"Bearer {owner_token}"}
            )
            strategy_id = r.json()["id"]

            other_headers = {"Authorization": f"Bearer {other_token}"}
            r = client.get(f"/strategies/{strategy_id}/versions", headers=other_headers)
            assert r.status_code == 404, r.text

            r = client.get(f"/strategies/{strategy_id}/versions/1", headers=other_headers)
            assert r.status_code == 404, r.text
        finally:
            await _cleanup([owner_id, other_id], [uuid.UUID(strategy_id)] if strategy_id else [])
