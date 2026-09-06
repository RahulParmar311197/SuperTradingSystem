"""Pluggable AI client interface (blueprint §21 "Use an LLM for..." / §110
"AI Failure"). Set AI_PROVIDER=anthropic and AI_API_KEY to enable the
Claude-backed implementation (app.ai.providers.anthropic_client); with no
key configured, `get_ai_client` returns `NullAIClient`, which fails closed
per §110 ("no AI -> no trade") rather than faking a response."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.config import Settings


class AIUnavailableError(Exception):
    """Raised when an AI-mandatory operation is requested but no provider
    is configured. Per blueprint §110, this must mean 'no new trade', never
    a silent fallback to a guess."""


class AIProviderError(Exception):
    """Raised when a *configured* provider's API call itself failed (rate
    limit, timeout, connection error, non-2xx status) -- distinct from
    `AIUnavailableError` (no provider configured at all) and from a
    `ValueError` (the provider answered, but its content wasn't usable,
    e.g. not valid JSON). Concrete `AIClient` implementations should catch
    their SDK's own exception types and re-raise as this, so callers have
    exactly one type to handle for "the call to the provider failed"
    regardless of which provider is configured."""


class AIClient(ABC):
    @abstractmethod
    async def complete_json(self, prompt: str, system: str | None = None) -> dict:
        """Sends `prompt` to the model and returns its response parsed as
        JSON. Implementations must not fabricate a response on failure —
        raise instead, so callers can apply blueprint §110's "no AI -> no
        trade" rule."""


class NullAIClient(AIClient):
    async def complete_json(self, prompt: str, system: str | None = None) -> dict:
        raise AIUnavailableError("No AI provider is configured for this environment")


def get_ai_client(settings: Settings) -> AIClient:
    if settings.ai_provider == "none" or not settings.ai_api_key:
        return NullAIClient()
    if settings.ai_provider == "anthropic":
        from app.ai.providers.anthropic_client import AnthropicAIClient

        return AnthropicAIClient(api_key=settings.ai_api_key, model=settings.ai_model)
    raise NotImplementedError(
        f"AI provider '{settings.ai_provider}' has no adapter implemented yet. "
        "Implement an AIClient subclass for it before enabling AI features."
    )
