"""Concrete `AIClient` backed by the Claude API (blueprint §21, §30).

Selected via `AI_PROVIDER=anthropic` + `AI_API_KEY` in settings (see
app.ai.client.get_ai_client). Every caller still goes through
app.ai.strategy_builder / app.ai.validation for schema and trade-proposal
validation — this class only knows how to get JSON text out of Claude.
"""

from __future__ import annotations

import json
import re

from anthropic import AsyncAnthropic

from app.ai.client import AIClient

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class AIResponseParseError(ValueError):
    """The AI responded, but its content wasn't parseable JSON."""


def _extract_json(text: str) -> dict:
    fenced = _JSON_FENCE_RE.search(text)
    candidate = fenced.group(1) if fenced else text
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise AIResponseParseError(f"AI response was not valid JSON: {exc}") from exc


class AnthropicAIClient(AIClient):
    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5") -> None:
        self._client = AsyncAnthropic(api_key=api_key)
        self.model = model

    async def complete_json(self, prompt: str, system: str | None = None) -> dict:
        response = await self._client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=system or "Respond with JSON only, no prose, no markdown fences.",
            messages=[{"role": "user", "content": prompt}],
        )
        text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        return _extract_json("".join(text_parts))
