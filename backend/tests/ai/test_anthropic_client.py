"""Tests for the Anthropic-backed AIClient. No network calls are made —
`messages.create` is monkeypatched, since we don't have (and shouldn't
need) a real API key to verify our own response-parsing logic."""

from __future__ import annotations

from types import SimpleNamespace

import anthropic
import httpx
import pytest

from app.ai.client import AIProviderError
from app.ai.providers.anthropic_client import AIResponseParseError, AnthropicAIClient, _extract_json


def test_extract_json_handles_plain_json():
    assert _extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_strips_markdown_fences():
    text = 'Here you go:\n```json\n{"a": 1, "b": [1, 2]}\n```'
    assert _extract_json(text) == {"a": 1, "b": [1, 2]}


def test_extract_json_raises_on_garbage():
    with pytest.raises(AIResponseParseError):
        _extract_json("this is not json at all")


@pytest.mark.asyncio
async def test_complete_json_extracts_text_blocks_and_parses(monkeypatch):
    client = AnthropicAIClient(api_key="sk-fake-key-for-testing")

    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text='```json\n{"name": "Test Strategy"}\n```')]
    )

    async def fake_create(**kwargs):
        assert kwargs["model"] == client.model
        assert kwargs["messages"][0]["content"] == "hello"
        return fake_response

    monkeypatch.setattr(client._client.messages, "create", fake_create)

    result = await client.complete_json("hello")
    assert result == {"name": "Test Strategy"}


@pytest.mark.asyncio
async def test_complete_json_wraps_api_errors_as_ai_provider_error(monkeypatch):
    # Regression test: a rate limit, timeout, connection error, or any
    # other non-2xx response from the Anthropic API used to propagate as a
    # raw `anthropic.APIError` -- callers in app/api/ai.py only handled
    # `AIUnavailableError` (raised solely when no provider is configured
    # at all), so this, the far more likely failure mode in a real
    # deployment, fell through to a bare 500 with no audit row written.
    client = AnthropicAIClient(api_key="sk-fake-key-for-testing")

    async def fake_create_raises(**kwargs):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))

    monkeypatch.setattr(client._client.messages, "create", fake_create_raises)

    with pytest.raises(AIProviderError):
        await client.complete_json("hello")
