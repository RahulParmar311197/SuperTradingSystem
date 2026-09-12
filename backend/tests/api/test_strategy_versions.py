import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

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


async def test_creating_or_updating_a_strategy_rejects_an_unresolvable_entry_type(require_infra):
    # Regression test: `EntryConfig.type` was an unvalidated `str`, so
    # POST /strategies returned 201 for `entry.type="limit"` (or a case
    # typo of a real type) and persisted it. `_resolve_entry_and_stop`
    # then read it with bare equality tests and an implicit `else`,
    # silently trading that strategy as a *market* entry at the current
    # price -- a different entry, a different stop, and a fill on candles
    # where the strategy as written would not have traded.
    with TestClient(app) as client:
        token, user_id = await _register(client, "stratentry")
        headers = {"Authorization": f"Bearer {token}"}
        strategy_id = None
        try:
            for bad in ("limit", "fvg-retest", "retest"):
                payload = _strategy_payload("Bad entry type")
                payload["entry"] = {"type": bad}
                r = client.post("/strategies", json=payload, headers=headers)
                assert r.status_code == 422, f"{bad!r} -> {r.status_code}: {r.text}"

            # Nothing was persisted by any of those attempts.
            r = client.get("/strategies", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json() == []

            # A real entry type still works, and a case typo of one is
            # normalized rather than silently swapped for a market entry.
            payload = _strategy_payload("Good entry type")
            payload["entry"] = {"type": "FVG_RETEST"}
            r = client.post("/strategies", json=payload, headers=headers)
            assert r.status_code == 201, r.text
            strategy_id = r.json()["id"]
            assert r.json()["definition"]["entry"]["type"] == "fvg_retest"

            # PUT validates through the same model.
            update = _strategy_payload("Good entry type")
            update["entry"] = {"type": "market_order"}
            r = client.put(f"/strategies/{strategy_id}", json=update, headers=headers)
            assert r.status_code == 422, r.text
        finally:
            await _cleanup([user_id], [uuid.UUID(strategy_id)] if strategy_id else [])


async def test_library_endpoints_list_and_install_shipped_strategies(require_infra):
    # The library is only useful if a user can actually get one. Declared
    # before `/{strategy_id}`, so GET /strategies/library must resolve to
    # the literal route rather than being parsed as a UUID.
    with TestClient(app) as client:
        email = f"lib-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Lib"})
        assert r.status_code == 201, r.text
        user_id = uuid.UUID(r.json()["id"])
        token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        try:
            r = client.get("/strategies/library", headers=headers)
            assert r.status_code == 200, r.text
            entries = r.json()
            assert len(entries) >= 5
            keys = {e["key"] for e in entries}
            assert "bullish_liquidity_sweep" in keys

            r = client.post("/strategies/library/bullish_liquidity_sweep", headers=headers)
            assert r.status_code == 201, r.text
            installed = r.json()
            assert installed["name"] == "Bullish Liquidity Sweep"

            # It is a copy the user owns, and it shows up as theirs.
            r = client.get("/strategies", headers=headers)
            assert r.status_code == 200, r.text
            assert any(s["id"] == installed["id"] for s in r.json())

            r = client.post("/strategies/library/no_such_strategy", headers=headers)
            assert r.status_code == 404, r.text
        finally:
            async with async_session_factory() as db:
                from app.database.models.strategy import Strategy as SRow
                from app.database.models.strategy import StrategyVersion as SVRow
                from app.database.models.users import UserSession

                ids = (await db.execute(select(SRow.id).where(SRow.user_id == user_id))).scalars().all()
                for sid in ids:
                    await db.execute(delete(SVRow).where(SVRow.strategy_id == sid))
                await db.execute(delete(SRow).where(SRow.user_id == user_id))
                await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
                await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
