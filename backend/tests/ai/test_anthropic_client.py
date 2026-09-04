"""Tests for the Anthropic-backed AIClient. No network calls are made —
`messages.create` is monkeypatched, since we don't have (and shouldn't
need) a real API key to verify our own response-parsing logic."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
