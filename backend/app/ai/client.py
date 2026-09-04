"""Pluggable AI client interface (blueprint §21 "Use an LLM for..." / §110
"AI Failure"). No provider is wired up in this environment — supply an
API key and a concrete `AIClient` implementation before enabling AI
features in production."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.config import Settings


class AIUnavailableError(Exception):
    """Raised when an AI-mandatory operation is requested but no provider
    is configured. Per blueprint §110, this must mean 'no new trade', never
    a silent fallback to a guess."""


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
    raise NotImplementedError(
        f"AI provider '{settings.ai_provider}' has no adapter implemented yet. "
        "Implement an AIClient subclass for it before enabling AI features."
    )
