"""Natural-language -> Strategy DSL translation (blueprint §32).

The AI (via `AIClient`) only ever produces the JSON shape defined by
`StrategyDefinition`; this module is the backend validation gate before
that JSON is trusted as a tradable strategy. The AI never generates
executable code (§32's explicit prohibition).
"""

from __future__ import annotations

from pydantic import ValidationError

from app.ai.client import AIClient
from app.strategy.dsl import StrategyDefinition

_SYSTEM_PROMPT = (
    "You translate a trader's natural-language setup description into the Strategy DSL JSON "
    "schema. Only use condition types, operators, and entry types the schema defines. Never "
    "output executable code — JSON only."
)


class StrategyBuilderError(Exception):
    def __init__(self, message: str, raw_output: dict | None = None) -> None:
        super().__init__(message)
        self.raw_output = raw_output


def parse_strategy_json(raw: dict) -> StrategyDefinition:
    try:
        return StrategyDefinition.model_validate(raw)
    except ValidationError as exc:
        raise StrategyBuilderError(f"AI strategy output failed schema validation: {exc}", raw_output=raw) from exc


async def build_strategy_from_description(description: str, market: str, timeframe: str, ai_client: AIClient) -> StrategyDefinition:
    prompt = (
        f"{_SYSTEM_PROMPT}\n\nMarket: {market}\nTimeframe: {timeframe}\n"
        f"Trader's description: {description}\n\nRespond with StrategyDefinition JSON only."
    )
    raw = await ai_client.complete_json(prompt, system=_SYSTEM_PROMPT)
    return parse_strategy_json(raw)
