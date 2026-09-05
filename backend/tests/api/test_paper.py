import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.instruments import Instrument, MarketType
from app.database.models.notifications import Notification
from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.trading import ExecutionMode, Trade
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio

# Same bullish sweep+FVG dataset already proven to open and then run to
# target in tests/workers/test_auto_trade_worker.py.
_SETUP = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),  # retraces into the FVG -> entry
    (104, 130, 104, 128),  # runs hard to target
]


async def _register(client: TestClient, label: str) -> tuple[str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    return token, user_id


async def _make_instrument() -> Instrument:
    async with async_session_factory() as db:
        instrument = Instrument(
            symbol=f"PPR{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ"
        )
        db.add(instrument)
        await db.commit()
        await db.refresh(instrument)
        return instrument


async def _create_strategy(client: TestClient, headers: dict, symbol: str) -> uuid.UUID:
    payload = {
        "name": "Bullish FVG retest",
        "market": symbol,
        "timeframe": "15m",
        "direction": "bullish",
        "conditions": [{"type": "fvg", "direction": "bullish"}],
        "entry": {"type": "fvg_retest"},
        "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
    }
    r = client.post("/strategies", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


async def _cleanup(user_ids: list[uuid.UUID], strategy_ids: list[uuid.UUID], instrument_id: uuid.UUID | None = None) -> None:
    async with async_session_factory() as db:
        for user_id in user_ids:
            await db.execute(delete(Trade).where(Trade.user_id == user_id))
            await db.execute(delete(Notification).where(Notification.user_id == user_id))
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        for strategy_id in strategy_ids:
            await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.id.in_(strategy_ids)))
        for user_id in user_ids:
            await db.execute(delete(User).where(User.id == user_id))
        if instrument_id is not None:
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.commit()


async def test_create_get_feed_and_close_paper_session(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register(client, "paperowner")
        headers = {"Authorization": f"Bearer {token}"}
        instrument = await _make_instrument()
        strategy_id = await _create_strategy(client, headers, instrument.symbol)

        try:
            r = client.post(
                "/paper", json={"strategy_id": str(strategy_id), "symbol": instrument.symbol, "starting_balance": 50000.0}, headers=headers
            )
            assert r.status_code == 200, r.text
            session_id = r.json()["session_id"]
            assert r.json()["balance"] == 50000.0

            r = client.get(f"/paper/{session_id}", headers=headers)
            assert r.status_code == 200, r.text

            r = client.post(
                f"/paper/{session_id}/candle",
                json={"timestamp": "2026-01-05T09:15:00Z", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10},
                headers=headers,
            )
            assert r.status_code == 200, r.text

            r = client.delete(f"/paper/{session_id}", headers=headers)
            assert r.status_code == 204, r.text

            r = client.get(f"/paper/{session_id}", headers=headers)
            assert r.status_code == 404, r.text
        finally:
            await _cleanup([user_id], [strategy_id], instrument.id)


async def test_user_cannot_access_another_users_paper_session(require_infra):
    # Regression test: _get_session had no ownership check at all -- any
    # authenticated user who knew/guessed another user's paper session
    # UUID could view its state and feed candles into it, mutating
    # someone else's paper trading session. The exact same bug already
    # fixed for /replay/* in an earlier round, but missed here.
    with TestClient(app) as client:
        owner_token, owner_id = await _register(client, "paperowner2")
        other_token, other_id = await _register(client, "paperother")
        owner_headers = {"Authorization": f"Bearer {owner_token}"}
        instrument = await _make_instrument()
        strategy_id = await _create_strategy(client, owner_headers, instrument.symbol)

        try:
            r = client.post(
                "/paper", json={"strategy_id": str(strategy_id), "symbol": instrument.symbol}, headers=owner_headers
            )
            session_id = r.json()["session_id"]

            other_headers = {"Authorization": f"Bearer {other_token}"}
            r = client.get(f"/paper/{session_id}", headers=other_headers)
            assert r.status_code == 404, r.text

            r = client.post(
                f"/paper/{session_id}/candle",
                json={"timestamp": "2026-01-05T09:15:00Z", "open": 100, "high": 102, "low": 99, "close": 101, "volume": 10},
                headers=other_headers,
            )
            assert r.status_code == 404, r.text

            r = client.delete(f"/paper/{session_id}", headers=other_headers)
            assert r.status_code == 404, r.text

            # The owner can still use their own session -- the other
            # user's failed attempts didn't close or corrupt it.
            r = client.get(f"/paper/{session_id}", headers=owner_headers)
            assert r.status_code == 200, r.text
        finally:
            await _cleanup([owner_id, other_id], [strategy_id], instrument.id)


async def test_paper_trading_persists_trade_and_notification_on_close(require_infra):
    # Regression test: a manual paper trading session's closed positions
    # were never journaled anywhere -- only AutoTradeSupervisor (driving
    # this exact same PaperTradingEngine) wrote a Trade row + notification
    # on close. After DELETE /paper/{id} (or a process restart, since
    # _SESSIONS is in-memory), the entire session -- including realized
    # P&L from a stop/target exit -- left no record anywhere.
    with TestClient(app) as client:
        token, user_id = await _register(client, "paperjournal")
        headers = {"Authorization": f"Bearer {token}"}
        instrument = await _make_instrument()
        strategy_id = await _create_strategy(client, headers, instrument.symbol)

        try:
            r = client.post("/paper", json={"strategy_id": str(strategy_id), "symbol": instrument.symbol}, headers=headers)
            assert r.status_code == 200, r.text
            session_id = r.json()["session_id"]

            start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
            for i, (o, h, low, c) in enumerate(_SETUP):
                ts = (start + timedelta(minutes=i)).isoformat()
                r = client.post(
                    f"/paper/{session_id}/candle",
                    json={"timestamp": ts, "open": o, "high": h, "low": low, "close": c, "volume": 10},
                    headers=headers,
                )
                assert r.status_code == 200, r.text

            # The position should have opened and then closed (hit
            # target) by the end of this sequence -- proven by no
            # position being open at the end, and by the Trade/Notification
            # assertions below.
            r = client.get(f"/paper/{session_id}", headers=headers)
            assert r.json()["open_position"] is None

            async with async_session_factory() as db:
                trade = (await db.execute(select(Trade).where(Trade.user_id == user_id))).scalar_one()
                assert trade.execution_mode == ExecutionMode.PAPER
                assert trade.instrument_id == instrument.id
                assert trade.strategy_id == strategy_id
                assert float(trade.pnl) != 0.0

                notification = (await db.execute(select(Notification).where(Notification.user_id == user_id))).scalar_one()
                assert "paper trade closed" in notification.title
        finally:
            await _cleanup([user_id], [strategy_id], instrument.id)
