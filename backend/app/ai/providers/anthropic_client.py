"""Concrete `AIClient` backed by the Claude API (blueprint §21, §30).

Selected via `AI_PROVIDER=anthropic` + `AI_API_KEY` in settings (see
app.ai.client.get_ai_client). Every caller still goes through
app.ai.strategy_builder / app.ai.validation for schema and trade-proposal
validation — this class only knows how to get JSON text out of Claude.
"""

from __future__ import annotations

import json
import re

import anthropic
from anthropic import AsyncAnthropic

from app.ai.client import AIClient, AIProviderError

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
        # Regression fix: a rate limit, timeout, connection error, or any
        # other non-2xx response from the Anthropic API used to propagate
        # as a raw `anthropic.APIError` -- a real-deployment failure mode
        # (this fires whenever a provider *is* configured) that callers in
        # app/api/ai.py only ever handled for the rarer "no provider
        # configured at all" case (`AIUnavailableError`), so it fell
        # through to the generic 500 handler with no `AIDecision`/`AIMessage`
        # audit row ever written. `anthropic.APIError` is the base class
        # for every exception this SDK raises (rate limits, timeouts,
        # connection errors, and non-2xx statuses all subclass it), so
        # catching it here and re-raising the shared `AIProviderError`
        # gives every caller exactly one type to handle regardless of
        # which specific SDK failure occurred.
        try:
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=system or "Respond with JSON only, no prose, no markdown fences.",
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise AIProviderError(f"Anthropic API call failed: {exc}") from exc
        text_parts = [block.text for block in response.content if getattr(block, "type", None) == "text"]
        return _extract_json("".join(text_parts))
