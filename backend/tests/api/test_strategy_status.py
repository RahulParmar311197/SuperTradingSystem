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


def _strategy_payload(name: str) -> dict:
    return {
        "name": name,
        "market": "NIFTY",
        "timeframe": "15m",
        "direction": "bullish",
        "conditions": [{"type": "fvg", "direction": "bullish"}],
        "entry": {"type": "fvg_retest"},
        "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
    }


async def test_new_strategy_is_never_eligible_for_auto_trading_by_default(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register(client, "stratstatus")
        headers = {"Authorization": f"Bearer {token}"}
        strategy_id = None
        try:
            r = client.post("/strategies", json=_strategy_payload("Fresh"), headers=headers)
            assert r.status_code == 201, r.text
            strategy_id = r.json()["id"]
            assert r.json()["eligible_for_auto_trading"] is False
            assert r.json()["is_active"] is True
        finally:
            await _cleanup([user_id], [uuid.UUID(strategy_id)] if strategy_id else [])


async def test_promoting_a_strategy_requires_auto_trade_permission_and_confirm(require_infra):
    # Regression test: nothing anywhere in the API ever wrote
    # `eligible_for_auto_trading` -- create_strategy left it at the DB
    # default (False) and update_strategy (PUT) only ever touched
    # definition/name/version. AutoTradeSupervisor filters strategies
    # WHERE eligible_for_auto_trading IS TRUE, so that filter was
    # unsatisfiable for every real user regardless of the account-level
    # POST /auto-trading/enable switch -- autonomous trading could never
    # place a single order. PATCH /strategies/{id}/status is the missing
    # promotion step (blueprint §77).
    with TestClient(app) as client:
        token, user_id = await _register(client, "stratpromote")
        headers = {"Authorization": f"Bearer {token}"}
        strategy_id = None
        try:
            r = client.post("/strategies", json=_strategy_payload("To promote"), headers=headers)
            strategy_id = r.json()["id"]

            # No AUTO_TRADE permission yet -> 403, even with confirm=true.
            r = client.patch(
                f"/strategies/{strategy_id}/status",
                json={"eligible_for_auto_trading": True, "confirm": True},
                headers=headers,
            )
            assert r.status_code == 403, r.text

            r = client.post("/trading-permissions/grant", json={"permission": "AUTO_TRADE", "confirm": True}, headers=headers)
            assert r.status_code == 200, r.text

            # Has the permission now, but no confirm -> 400.
            r = client.patch(
                f"/strategies/{strategy_id}/status", json={"eligible_for_auto_trading": True}, headers=headers
            )
            assert r.status_code == 400, r.text

            r = client.patch(
                f"/strategies/{strategy_id}/status",
                json={"eligible_for_auto_trading": True, "confirm": True},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["eligible_for_auto_trading"] is True

            r = client.get(f"/strategies/{strategy_id}", headers=headers)
            assert r.json()["eligible_for_auto_trading"] is True

            # Demoting back to False needs neither the permission check nor
            # confirm -- only promoting is guarded, the same asymmetry as
            # POST /auto-trading/enable vs. /disable.
            r = client.patch(
                f"/strategies/{strategy_id}/status", json={"eligible_for_auto_trading": False}, headers=headers
            )
            assert r.status_code == 200, r.text
            assert r.json()["eligible_for_auto_trading"] is False

            r = client.patch(f"/strategies/{strategy_id}/status", json={"is_active": False}, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["is_active"] is False
        finally:
            await _cleanup([user_id], [uuid.UUID(strategy_id)] if strategy_id else [])


async def test_user_cannot_update_another_users_strategy_status(require_infra):
    with TestClient(app) as client:
        owner_token, owner_id = await _register(client, "stratstatusowner")
        other_token, other_id = await _register(client, "stratstatusother")
        strategy_id = None
        try:
            r = client.post(
                "/strategies", json=_strategy_payload("Private"), headers={"Authorization": f"Bearer {owner_token}"}
            )
            strategy_id = r.json()["id"]

            other_headers = {"Authorization": f"Bearer {other_token}"}
            r = client.patch(f"/strategies/{strategy_id}/status", json={"is_active": False}, headers=other_headers)
            assert r.status_code == 404, r.text
        finally:
            await _cleanup([owner_id, other_id], [uuid.UUID(strategy_id)] if strategy_id else [])
