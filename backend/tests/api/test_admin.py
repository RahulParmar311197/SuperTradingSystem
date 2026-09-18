import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.redis import (
    account_halt_reason,
    clear_account_kill,
    clear_global_kill,
    clear_strategy_kill,
    halt_account,
    is_account_killed,
    is_global_killed,
    is_strategy_killed,
    resume_account,
)
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
                "/admin/ai-decisions",
                "/admin/kill-switch",
            ):
                r = client.get(path, headers=headers)
                assert r.status_code == 403, f"{path}: {r.text}"

            r = client.post("/admin/accounts/some-account/resume", json={"confirm": True}, headers=headers)
            assert r.status_code == 403, r.text

            r = client.post("/admin/kill-switch/global", json={"confirm": True}, headers=headers)
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

            r = client.get("/admin/ai-decisions", headers=headers)
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


async def test_admin_can_view_and_trigger_the_three_level_kill_switch(require_infra):
    # Regression test: blueprint §58's three-level kill switch
    # (app/risk/kill_switch.py) had no admin-reachable way to ever be
    # triggered -- `KillSwitchState` was a plain in-memory dataclass that
    # nothing outside a unit test ever called kill_global/kill_account/
    # kill_strategy on, so `RiskEngine.evaluate`'s "kill_switch" check was
    # permanently a no-op. These are the endpoints that make it real,
    # backed by Redis so a kill here is visible to every RiskEngine in
    # every process (see app.risk.kill_switch.load_kill_switch_state).
    with TestClient(app) as client:
        admin_token, admin_id = await _register(client, "admin")

        async with async_session_factory() as db:
            admin_user = await db.get(User, admin_id)
            admin_user.role = UserRole.ADMIN
            await db.commit()

        headers = {"Authorization": f"Bearer {admin_token}"}
        account_id = f"killtest-{uuid.uuid4().hex[:8]}"
        strategy_id = f"strat-{uuid.uuid4().hex[:8]}"
        try:
            r = client.get("/admin/kill-switch", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["global_kill"] is False

            # Requires confirm=true, on every level.
            r = client.post("/admin/kill-switch/global", json={"confirm": False}, headers=headers)
            assert r.status_code == 400, r.text
            assert await is_global_killed() is False

            r = client.post("/admin/kill-switch/global", json={"confirm": True}, headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["global_kill"] is True
            assert await is_global_killed() is True

            r = client.delete("/admin/kill-switch/global", headers=headers)
            assert r.status_code == 200, r.text
            assert r.json()["global_kill"] is False
            assert await is_global_killed() is False

            r = client.post(f"/admin/kill-switch/account/{account_id}", json={"confirm": True}, headers=headers)
            assert r.status_code == 200, r.text
            assert account_id in r.json()["killed_accounts"]
            assert await is_account_killed(account_id) is True

            r = client.delete(f"/admin/kill-switch/account/{account_id}", headers=headers)
            assert r.status_code == 200, r.text
            assert account_id not in r.json()["killed_accounts"]
            assert await is_account_killed(account_id) is False

            r = client.post(f"/admin/kill-switch/strategy/{strategy_id}", json={"confirm": True}, headers=headers)
            assert r.status_code == 200, r.text
            assert strategy_id in r.json()["killed_strategies"]
            assert await is_strategy_killed(strategy_id) is True

            r = client.delete(f"/admin/kill-switch/strategy/{strategy_id}", headers=headers)
            assert r.status_code == 200, r.text
            assert strategy_id not in r.json()["killed_strategies"]
            assert await is_strategy_killed(strategy_id) is False
        finally:
            await clear_global_kill()
            await clear_account_kill(account_id)
            await clear_strategy_kill(strategy_id)
            await _cleanup(admin_id)


# --- POST /admin/backfill: the first caller backfill_candles ever had ------


async def _make_admin(client: TestClient, label: str) -> tuple[dict, uuid.UUID]:
    token, user_id = await _register(client, label)
    async with async_session_factory() as db:
        admin_user = await db.get(User, user_id)
        admin_user.role = UserRole.ADMIN
        await db.commit()
    return {"Authorization": f"Bearer {token}"}, user_id


class _StubMarketData:
    """Stands in for `UpstoxMarketData`. Records the call and returns bars.

    Deliberately not a mock of the HTTP layer: what this round wires is the
    *call*, and the provider's own parsing is covered by its own tests.
    """

    def __init__(self, candles):
        self.candles = candles
        self.calls = []

    async def get_historical_candles(self, key, interval, from_date, to_date):
        self.calls.append((key, interval, from_date, to_date))
        return self.candles


async def test_backfill_writes_real_candles_through_the_endpoint(require_infra, monkeypatch):
    """Behavioural proof, and the round's whole point.

    `backfill_candles` had **no caller anywhere in app/** -- the function,
    the read-only client and the instrument-key resolution all existed and
    nothing invoked any of them, so the only candles a deployment could
    hold were whatever a test had inserted. This drives the real route and
    asserts the rows land in the store the scanner and the autonomous loop
    actually read.
    """
    from datetime import datetime, timedelta, timezone

    import app.api.admin as admin_module
    from app.database.models.instruments import Instrument, MarketType
    from app.database.models.market import Candle as CandleRow
    from app.market.repository import get_candles
    from app.smc.types import Candle as SMCCandle

    start = datetime(2026, 1, 5, 3, 45, tzinfo=timezone.utc)
    bars = [SMCCandle(start + timedelta(minutes=i), 100.0, 101.0, 99.0, 100.5, 5000.0) for i in range(4)]
    stub = _StubMarketData(bars)
    monkeypatch.setattr(admin_module, "market_data_provider_or_reason", lambda: (stub, ""))

    with TestClient(app) as client:
        headers, admin_id = await _make_admin(client, "backfilladmin")
        async with async_session_factory() as db:
            instrument = Instrument(
                symbol=f"BF{uuid.uuid4().hex[:6].upper()}",
                exchange="NSE",
                market=MarketType.EQUITY,
                instrument_type="EQ",
                broker_instrument_key="NSE_EQ|INE000A01001",
            )
            db.add(instrument)
            await db.commit()
            await db.refresh(instrument)
            instrument_id, symbol = instrument.id, instrument.symbol

        try:
            r = client.post(
                "/admin/backfill",
                json={"symbol": symbol, "timeframe": "1m", "from_date": "2026-01-05", "to_date": "2026-01-06"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            assert r.json()["candles_written"] == 4

            # The bars are in the store every downstream reader uses.
            async with async_session_factory() as db:
                stored = await get_candles(db, instrument_id, "1m")
            assert len(stored) == 4

            # ... and the provider was asked for the instrument's own key
            # and the provider's own interval name, not this codebase's.
            assert stub.calls == [("NSE_EQ|INE000A01001", "1minute", *stub.calls[0][2:])]
        finally:
            async with async_session_factory() as db:
                await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
                await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
                await db.commit()
            await _cleanup(admin_id)


async def test_backfill_without_a_provider_says_what_to_configure(require_infra, monkeypatch):
    """Control. A deployment with no token is misconfigured, not broken:
    503 naming the remedy, never a 500 with a traceback."""
    import app.api.admin as admin_module
    import app.market.providers.factory as factory

    # Drive the real factory with no token rather than hand-writing the
    # reason string here: the point is that the message an operator sees
    # is the one the factory actually produces.
    class _NoToken:
        upstox_data_access_token = None

    monkeypatch.setattr(factory, "get_settings", lambda: _NoToken())
    monkeypatch.setattr(
        admin_module, "market_data_provider_or_reason", factory.market_data_provider_or_reason
    )

    with TestClient(app) as client:
        headers, admin_id = await _make_admin(client, "noprovider")
        try:
            r = client.post(
                "/admin/backfill",
                json={"symbol": "ANY", "timeframe": "1m", "from_date": "2026-01-05", "to_date": "2026-01-06"},
                headers=headers,
            )
            assert r.status_code == 503, r.text
            assert "UPSTOX_DATA_ACCESS_TOKEN" in r.text
        finally:
            await _cleanup(admin_id)


async def test_backfill_is_admin_only(require_infra):
    """Control. This endpoint spends provider quota and writes to the
    candle store every strategy reads; it must sit behind the same gate as
    the rest of /admin."""
    with TestClient(app) as client:
        token, user_id = await _register(client, "notadminbf")
        try:
            r = client.post(
                "/admin/backfill",
                json={"symbol": "ANY", "timeframe": "1m", "from_date": "2026-01-05", "to_date": "2026-01-06"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert r.status_code == 403, r.text
        finally:
            await _cleanup(user_id)
