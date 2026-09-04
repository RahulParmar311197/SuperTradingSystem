import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.database.models.backtest import Backtest as BacktestRow
from app.database.models.backtest import BacktestMetrics as BacktestMetricsRow
from app.database.models.backtest import BacktestTrade as BacktestTradeRow
from app.database.models.instruments import Instrument, MarketType
from app.database.models.market import Candle as CandleRow
from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio

_UNIT = [
    (100, 100, 99, 100),
    (100, 102, 100, 101),
    (101, 103, 100, 102),
    (102, 102, 97, 98),
    (98, 99, 96, 97),
    (97, 100, 96, 99),
    (99, 108, 99, 107),
    (107, 110, 106, 109),
    (109, 109, 103, 104),
    (104, 130, 104, 128),
]


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID, strategy_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        backtest_ids = (await db.execute(select(BacktestRow.id).where(BacktestRow.instrument_id == instrument_id))).scalars().all()
        for backtest_id in backtest_ids:
            await db.execute(delete(BacktestMetricsRow).where(BacktestMetricsRow.backtest_id == backtest_id))
            await db.execute(delete(BacktestTradeRow).where(BacktestTradeRow.backtest_id == backtest_id))
        await db.execute(delete(BacktestRow).where(BacktestRow.instrument_id == instrument_id))
        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        await db.execute(delete(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.id == strategy_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_run_backtest_returns_metrics_without_crashing(require_infra):
    # Regression test: POST /backtest previously 500'd on every call
    # (`BacktestMetricsResult` is `@dataclass(slots=True)`, and
    # `metrics.__dict__` raises AttributeError on it) because no test had
    # ever actually exercised this endpoint before.
    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    ohlc = _UNIT * 4
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(ohlc)]

    async with async_session_factory() as db:
        instrument = Instrument(symbol=f"BTR{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", candles)

    with TestClient(app) as client:
        email = f"btrun-{uuid.uuid4().hex[:8]}@example.com"
        r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "Backtest Run"})
        assert r.status_code == 201, r.text
        r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
        token = r.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        from app.auth.security import TokenType, decode_token

        user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))

        strategy_payload = {
            "name": "Bullish FVG retest",
            "market": instrument.symbol,
            "timeframe": "15m",
            "direction": "bullish",
            "conditions": [{"type": "fvg", "direction": "bullish"}],
            "entry": {"type": "fvg_retest"},
            "risk": {"risk_percent": 1.0, "minimum_rr": 2.0},
        }
        r = client.post("/strategies", json=strategy_payload, headers=headers)
        assert r.status_code == 201, r.text
        strategy_id = r.json()["id"]

        try:
            r = client.post(
                "/backtest",
                json={
                    "strategy_id": strategy_id,
                    "instrument_id": str(instrument_id),
                    "timeframe": "15m",
                    "start_date": start.isoformat(),
                    "end_date": (start + timedelta(minutes=len(ohlc))).isoformat(),
                },
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            for field in ("total_return", "net_profit", "win_rate", "max_drawdown", "total_trades", "equity_curve", "drawdown_curve", "monthly_returns"):
                assert field in body
        finally:
            await _cleanup(user_id, instrument_id, uuid.UUID(strategy_id))
