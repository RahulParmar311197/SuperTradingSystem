"""AI trade-proposal validation (blueprint §81).

The AI may *propose* a trade, but every number it states is cross-checked
against the deterministic `StrategyEvaluationResult` computed by
`app.strategy.engine` — the AI is never trusted to have computed entry/
stop/RR correctly on its own (§32, §131 "AI ≠ Final Authority").

"Never trusted" has to include the *shape* of what the model sends back,
not just the values. `AIClient.complete_json` is annotated `-> dict`, but
all it actually guarantees is that the response parsed as JSON: a bare
array, a string, or an object whose `entry` is the text `"N/A"` are all
valid JSON, and nothing between the model and this function rejects them.
So every read below goes through a coercion that turns unusable content
into a validation error, which is what this module exists to produce —
never an exception. `app.api.ai.propose_trade` calls this *after* its own
try/except around the provider and *before* it writes the `AIDecision`
audit row, so an exception escaping here is a bare 500 with no record at
all of what the AI said: exactly the audit hole that endpoint already
closed for a failing provider call and unparseable content.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from app.strategy.engine import StrategyEvaluationResult

_PRICE_TOLERANCE_PCT = 0.5  # AI-stated prices may drift this much from the computed ones

# The one value in the response that carries the model's own verdict
# (see `_TRADE_PROPOSAL_SYSTEM_PROMPT` in app/api/ai.py, which requires
# "TRADE" or "NO_TRADE"). Compared case-insensitively after stripping,
# since that prompt constrains wording, not formatting.
_TRADE_DECISION = "TRADE"


@dataclass(slots=True)
class AIValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)


def _within_tolerance(a: float, b: float, tolerance_pct: float = _PRICE_TOLERANCE_PCT) -> bool:
    if b == 0:
        return a == 0
    return abs(a - b) / abs(b) * 100 <= tolerance_pct


def _as_float(value: object) -> float | None:
    """The AI's idea of a number, or `None` when it isn't one.

    Booleans are rejected outright rather than riding `float(True) == 1.0`
    into a price, and `inf`/`nan` (which `float("inf")` accepts) are
    rejected because no entry, stop or ratio in this domain is either —
    both would sail through every comparison below as a silent pass or a
    silent mismatch instead of being named as the malformed input they
    are."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _numeric_field(proposal: dict, key: str, errors: list[str]) -> float | None:
    value = _as_float(proposal.get(key))
    if value is None:
        errors.append(f"Proposed {key} {proposal.get(key)!r} is not a number")
    return value


def validate_ai_trade_proposal(
    proposal: object,
    deterministic_result: StrategyEvaluationResult,
    instrument_tradable: bool,
    max_risk_percent: float,
) -> AIValidationResult:
    errors: list[str] = []

    if not isinstance(proposal, dict):
        # Valid JSON that isn't an object at all — every read below would
        # raise `AttributeError` on the first `.get`.
        return AIValidationResult(valid=False, errors=[f"AI response was not a JSON object: {type(proposal).__name__}"])

    if not deterministic_result.matched:
        errors.append("No signal exists: the strategy conditions are not currently satisfied")
        return AIValidationResult(valid=False, errors=errors)

    # The AI's own go/no-go. Nothing else in the system reads it, so
    # without this check a model that answered "NO_TRADE" — while dutifully
    # echoing the deterministic entry/stop/RR it was told to match — came
    # back `valid: true`, i.e. a declined setup presented to the operator
    # as a validated proposal. Absence is treated the same as a decline:
    # a response that never states a verdict is not an endorsement
    # (blueprint §110 "no AI -> no trade").
    decision = str(proposal.get("decision", "")).strip().upper()
    if decision != _TRADE_DECISION:
        errors.append(f"AI decision {proposal.get('decision')!r} is not '{_TRADE_DECISION}' — the AI did not propose this trade")

    proposed_direction = str(proposal.get("direction", "")).lower()
    if proposed_direction != (deterministic_result.direction or "").lower():
        errors.append(
            f"Proposed direction '{proposed_direction}' does not match the detected setup "
            f"'{deterministic_result.direction}'"
        )

    entry = _numeric_field(proposal, "entry", errors)
    if entry is not None and not _within_tolerance(entry, deterministic_result.entry):
        errors.append(f"Proposed entry {entry} does not match the computed entry {deterministic_result.entry}")

    stop = _numeric_field(proposal, "stop", errors)
    if stop is not None and not _within_tolerance(stop, deterministic_result.stop):
        errors.append(f"Proposed stop {stop} does not match the computed stop {deterministic_result.stop}")

    risk_reward = _numeric_field(proposal, "risk_reward", errors)
    if risk_reward is not None and risk_reward < deterministic_result.risk_reward - 1e-6:
        errors.append(
            f"Proposed risk/reward {risk_reward} is below the computed {deterministic_result.risk_reward}"
        )

    risk_percent = _numeric_field(proposal, "risk_percent", errors)
    if risk_percent is not None and (risk_percent <= 0 or risk_percent > max_risk_percent):
        errors.append(f"Proposed risk_percent {risk_percent} exceeds the maximum allowed {max_risk_percent}%")

    if not instrument_tradable:
        errors.append("Instrument is not currently tradable")

    return AIValidationResult(valid=len(errors) == 0, errors=errors)
