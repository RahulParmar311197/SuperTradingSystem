import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.ai.client import AIClient, AIProviderError
from app.database.models.ai import AIMessage
from app.database.models.instruments import Instrument, MarketType
from app.database.models.risk import AuditLog
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


class _FakeAIClient(AIClient):
    def __init__(self, response: dict) -> None:
        self.response = response
        self.last_prompt: str | None = None

    async def complete_json(self, prompt: str, system: str | None = None) -> dict:
        self.last_prompt = prompt
        return self.response


class _FailingAIClient(AIClient):
    """Stands in for a configured provider whose API call itself fails
    (rate limit, timeout, connection error) -- as opposed to `NullAIClient`
    (no provider configured at all, raises AIUnavailableError)."""

    async def complete_json(self, prompt: str, system: str | None = None) -> dict:
        raise AIProviderError("simulated rate limit")


async def _register(client: TestClient, label: str) -> tuple[str, uuid.UUID]:
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": label})
    assert r.status_code == 201, r.text
    r = client.post("/auth/login", json={"email": email, "password": "testpass123"})
    token = r.json()["access_token"]

    from app.auth.security import TokenType, decode_token

    user_id = uuid.UUID(decode_token(token, TokenType.ACCESS))
    return token, user_id


async def _cleanup(user_id: uuid.UUID, instrument_id: uuid.UUID | None = None) -> None:
    async with async_session_factory() as db:
        if instrument_id is not None:
            from app.database.models.market import Candle as CandleRow

            await db.execute(delete(CandleRow).where(CandleRow.instrument_id == instrument_id))
            await db.execute(delete(Instrument).where(Instrument.id == instrument_id))
        await db.execute(delete(AIMessage).where(AIMessage.user_id == user_id))
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_chat_returns_503_and_persists_both_messages_when_ai_unavailable(require_infra):
    with TestClient(app) as client:
        token, user_id = await _register(client, "chatunavail")
        headers = {"Authorization": f"Bearer {token}"}
        try:
            # Test env's AI_PROVIDER defaults to "none" -> NullAIClient -> AIUnavailableError.
            r = client.post("/ai/chat", json={"message": "Why is NIFTY bullish?"}, headers=headers)
            assert r.status_code == 503, r.text

            r = client.get("/ai/chat/history", headers=headers)
            assert r.status_code == 200, r.text
            messages = r.json()
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[0]["content"] == "Why is NIFTY bullish?"
            assert "unavailable" in messages[1]["content"].lower()
        finally:
            await _cleanup(user_id)


async def test_chat_returns_502_and_persists_both_messages_when_ai_provider_call_fails(require_infra, monkeypatch):
    # Regression test: a configured provider's API call itself failing
    # (rate limit, timeout, connection error -- AIProviderError) used to
    # propagate past this endpoint's `except AIUnavailableError` clause
    # entirely, so the assistant AIMessage row was never written, and
    # since the earlier user AIMessage was never committed before the
    # exception, both messages vanished from chat history with no visible
    # reply anywhere.
    with TestClient(app) as client:
        token, user_id = await _register(client, "chatproviderfail")
        headers = {"Authorization": f"Bearer {token}"}
        try:
            monkeypatch.setattr("app.api.ai.get_ai_client", lambda settings: _FailingAIClient())

            r = client.post("/ai/chat", json={"message": "Why is NIFTY bullish?"}, headers=headers)
            assert r.status_code == 502, r.text

            r = client.get("/ai/chat/history", headers=headers)
            assert r.status_code == 200, r.text
            messages = r.json()
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[0]["content"] == "Why is NIFTY bullish?"
            assert "simulated rate limit" in messages[1]["content"]
        finally:
            await _cleanup(user_id)


async def test_chat_grounds_reply_in_structured_facts_and_persists_history(require_infra, monkeypatch):
    with TestClient(app) as client:
        token, user_id = await _register(client, "chatgrounded")
        headers = {"Authorization": f"Bearer {token}"}

        start = datetime(2026, 1, 5, 9, 15, tzinfo=timezone.utc)
        candles = [Candle(start + timedelta(minutes=i), o, h, l, c, 100) for i, (o, h, l, c) in enumerate(_UNIT * 2)]
        async with async_session_factory() as db:
            instrument = Instrument(symbol=f"CHAT{uuid.uuid4().hex[:6].upper()}", exchange="NSE", market=MarketType.EQUITY, instrument_type="EQ")
            db.add(instrument)
            await db.flush()
            instrument_id = instrument.id
            await upsert_candles(db, instrument_id, "15m", candles)
            await db.commit()

        try:
            fake = _FakeAIClient({"reply": "Price is making higher highs and holding above the last bullish FVG."})
            monkeypatch.setattr("app.api.ai.get_ai_client", lambda settings: fake)

            r = client.post(
                "/ai/chat",
                json={"message": "Why is this bullish?", "instrument_id": str(instrument_id), "timeframe": "15m"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["role"] == "assistant"
            assert body["content"] == "Price is making higher highs and holding above the last bullish FVG."
            assert "Structured facts" in fake.last_prompt
            assert "Question: Why is this bullish?" in fake.last_prompt

            r = client.get("/ai/chat/history", headers=headers)
            messages = r.json()
            assert [m["role"] for m in messages] == ["user", "assistant"]
            assert messages[0]["content"] == "Why is this bullish?"
        finally:
            await _cleanup(user_id, instrument_id)


async def test_user_cannot_see_another_users_chat_history(require_infra):
    with TestClient(app) as client:
        first_token, first_id = await _register(client, "chatuser1")
        second_token, second_id = await _register(client, "chatuser2")
        try:
            client.post("/ai/chat", json={"message": "secret question"}, headers={"Authorization": f"Bearer {first_token}"})

            r = client.get("/ai/chat/history", headers={"Authorization": f"Bearer {second_token}"})
            assert r.status_code == 200, r.text
            assert r.json() == []
        finally:
            await _cleanup(first_id)
            await _cleanup(second_id)
