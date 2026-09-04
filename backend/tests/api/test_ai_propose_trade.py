import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from app.ai.client import AIClient
from app.database.models.ai import AIDecision
from app.database.models.instruments import Instrument, MarketType
from app.database.models.risk import AuditLog
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app
from app.market.repository import upsert_candles
from app.smc.types import Candle

pytestmark = pytest.mark.asyncio

# Same repeating bullish sweep+FVG pattern used in tests/api/test_backtest_validate.py.
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


class _FakeAIClient(AIClient):
    def __init__(self, response: dict) -> None:
        self.response = response

    async def complete_json(self, prompt: str, system: str | None = None) -> dict:
        return self.response


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID, strategy_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        from app.database.models.market import Candle as CandleRow

        await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
        await db.execute(delete(AIDecision).where(AIDecision.user_id == user_id))
        await db.execute(delete(StrategyRow).where(StrategyRow.id == strategy_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def _setup() -> tuple[TestClient, str, uuid.UUID, uuid.UUID, uuid.UUID]:
    client = TestClient(app)
    client.__enter__()

    start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
    candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(_UNIT * 4)]

    async with async_session_factory() as db:
        instrument = Instrument(symbol=f"AIPT{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
        db.add(instrument)
        await db.flush()
        instrument_id = instrument.id
        await upsert_candles(db, instrument_id, "15m", candles)

    email = f"aipropose-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "AI Propose Test"})
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
    strategy_id = uuid.UUID(r.json()["id"])

    return client, token, user_id, instrument_id, strategy_id


async def test_propose_trade_returns_503_and_records_a_decision_when_ai_unavailable(require_infra):
    client, token, user_id, instrument_id, strategy_id = await _setup()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        # Test env's AI_PROVIDER defaults to "none" -> NullAIClient -> AIUnavailableError.
        r = client.post(
            "/ai/propose-trade",
            json={"strategy_id": str(strategy_id), "instrument_id": str(instrument_id), "timeframe": "15m"},
            headers=headers,
        )
        assert r.status_code == 503, r.text

        async with async_session_factory() as db:
            decision = (await db.execute(select(AIDecision).where(AIDecision.user_id == user_id))).scalar_one()
            assert decision.validated is False
            assert decision.model is None
            assert "error" in decision.output
    finally:
        client.__exit__(None, None, None)
        await _cleanup(user_id, instrument_id, strategy_id)


async def test_propose_trade_persists_a_valid_decision_when_ai_matches_deterministic_result(require_infra, monkeypatch):
    client, token, user_id, instrument_id, strategy_id = await _setup()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        from app.database.session import async_session_factory as factory
        from app.market.repository import get_candles
        from app.strategy.context import EvaluationContext
        from app.strategy.dsl import StrategyDefinition
        from app.strategy.engine import StrategyEngine
        from app.ict.engine import ICTConfig, ICTEngine
        from app.smc.engine import SMCConfig, SMCEngine

        async with factory() as db:
            strategy_row = await db.get(StrategyRow, strategy_id)
            strategy = StrategyDefinition.model_validate(strategy_row.definition)
            candles = await get_candles(db, instrument_id, "15m")
        context = EvaluationContext(
            symbol="x",
            timeframe="15m",
            timestamp=candles[-1].timestamp,
            current_price=candles[-1].close,
            smc=SMCEngine(SMCConfig()).analyze(candles),
            ict=ICTEngine(ICTConfig()).analyze(candles),
        )
        deterministic = StrategyEngine().evaluate(strategy, context)
        assert deterministic.matched, "fixture strategy must actually match for this test to be meaningful"

        fake_proposal = {
            "decision": "TRADE",
            "direction": deterministic.direction,
            "entry": deterministic.entry,
            "stop": deterministic.stop,
            "risk_reward": deterministic.risk_reward,
            "risk_percent": 0.5,
            "reasoning": "matches the detected setup",
        }
        monkeypatch.setattr("app.api.ai.get_ai_client", lambda settings: _FakeAIClient(fake_proposal))

        r = client.post(
            "/ai/propose-trade",
            json={"strategy_id": str(strategy_id), "instrument_id": str(instrument_id), "timeframe": "15m", "max_risk_percent": 1.0},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["valid"] is True
        assert body["errors"] == []

        async with async_session_factory() as db:
            decision = (await db.execute(select(AIDecision).where(AIDecision.user_id == user_id))).scalar_one()
            assert decision.validated is True
            assert decision.output["direction"] == deterministic.direction
    finally:
        client.__exit__(None, None, None)
        await _cleanup(user_id, instrument_id, strategy_id)


async def test_explain_trade_returns_explanation_without_crashing(require_infra):
    # Regression test: POST /ai/explain-trade previously 500'd on every
    # call (`TradeExplanation` is `@dataclass(slots=True)`, and
    # `explanation.__dict__` raises AttributeError on it) because no test
    # had ever actually exercised this endpoint before.
    client, token, user_id, instrument_id, strategy_id = await _setup()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        r = client.post(
            "/ai/explain-trade",
            json={"strategy_id": str(strategy_id), "instrument_id": str(instrument_id), "timeframe": "15m"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        for field in ("market_context", "why_setup_exists", "conditions_satisfied", "conditions_missing", "risk", "invalidation", "potential_exit"):
            assert field in body
    finally:
        client.__exit__(None, None, None)
        await _cleanup(user_id, instrument_id, strategy_id)


async def test_propose_trade_flags_a_hallucinated_entry(require_infra, monkeypatch):
    client, token, user_id, instrument_id, strategy_id = await _setup()
    headers = {"Authorization": f"Bearer {token}"}
    try:
        fake_proposal = {
            "decision": "TRADE",
            "direction": "bullish",
            "entry": 99999.0,  # nowhere near the real computed entry
            "stop": 1.0,
            "risk_reward": 2.0,
            "risk_percent": 0.5,
            "reasoning": "hallucinated",
        }
        monkeypatch.setattr("app.api.ai.get_ai_client", lambda settings: _FakeAIClient(fake_proposal))

        r = client.post(
            "/ai/propose-trade",
            json={"strategy_id": str(strategy_id), "instrument_id": str(instrument_id), "timeframe": "15m"},
            headers=headers,
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["valid"] is False
        assert any("entry" in e for e in body["errors"])

        async with async_session_factory() as db:
            decision = (await db.execute(select(AIDecision).where(AIDecision.user_id == user_id))).scalar_one()
            assert decision.validated is False
    finally:
        client.__exit__(None, None, None)
        await _cleanup(user_id, instrument_id, strategy_id)
