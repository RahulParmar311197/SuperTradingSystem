"""Strategy DSL (blueprint §33-34): structured, backend-validated condition
trees the AI (or a human) can produce, but never raw executable code."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator


class ConditionType(StrEnum):
    TREND = "trend"
    BOS = "bos"
    MSS = "mss"
    CHOCH = "choch"
    LIQUIDITY_SWEEP = "liquidity_sweep"
    FVG = "fvg"
    ORDER_BLOCK = "order_block"
    PREMIUM_DISCOUNT = "premium_discount"
    VOLUME = "volume"
    VOLATILITY = "volatility"
    SESSION = "session"
    INDICATOR = "indicator"
    OPTIONS_IV = "options_iv"
    OPTIONS_OI = "options_oi"
    OPTIONS_GREEKS = "options_greeks"


class ConditionOperator(StrEnum):
    AND = "AND"
    OR = "OR"
    NOT = "NOT"
    GREATER_THAN = "GREATER_THAN"
    LESS_THAN = "LESS_THAN"
    CROSSES = "CROSSES"
    TOUCHES = "TOUCHES"
    WITHIN = "WITHIN"


class Condition(BaseModel):
    """A single leaf condition. `type` selects which evaluator handles it;
    the remaining fields are interpreted by that evaluator (see
    app/strategy/evaluator.py). Unused fields are simply ignored."""

    type: ConditionType
    direction: str | None = None  # "bullish" | "bearish", where relevant
    side: str | None = None  # "buy" | "sell", for liquidity_sweep
    zone: str | None = None  # "premium" | "discount", for premium_discount
    name: str | None = None  # indicator/session/greek name, e.g. "rsi", "delta"
    operator: ConditionOperator | None = None
    value: float | None = None
    min_value: float | None = None
    max_value: float | None = None
    lookback: int = 5  # how many recent candles/events count as "recent" for event-type conditions

    @field_validator("operator")
    @classmethod
    def _reject_unimplemented_boolean_operators(cls, v: ConditionOperator | None) -> ConditionOperator | None:
        # Blueprint §33 lists AND/OR/NOT among this DSL's operators, and
        # `ConditionOperator` declares all three -- but `Condition` is a
        # flat leaf (§34's own example, and `StrategyDefinition.conditions`'
        # docstring, both only ever show an implicit AND across the list),
        # and app/strategy/evaluator.py's `_numeric_compare` -- the only
        # reader of `Condition.operator` -- never implements AND/OR/NOT.
        # Before this validator, a strategy using one of them (the AI is
        # explicitly told it may, per app/ai/strategy_builder.py's system
        # prompt: "Only use ... operators ... the schema defines") passed
        # schema validation cleanly and was persisted as a normal-looking
        # strategy, but `evaluate_condition` silently fell through to
        # `return False` for that condition on every single candle
        # forever -- a strategy that can structurally never fire, with
        # nothing anywhere indicating why. Rejecting these three loudly at
        # validation time, rather than accepting them into a strategy that
        # can never trigger, is the honest behavior until real boolean
        # composition is actually implemented.
        if v in (ConditionOperator.AND, ConditionOperator.OR, ConditionOperator.NOT):
            raise ValueError(
                f"operator={v.value!r} is declared in the schema but not yet implemented by the evaluator -- "
                "a condition using it would silently never match. Use an implicit AND across separate "
                "conditions in the strategy's `conditions` list instead."
            )
        return v


class EntryConfig(BaseModel):
    type: str = "market"  # e.g. "market", "fvg_retest", "order_block_retest"
    params: dict = Field(default_factory=dict)


class RiskConfig(BaseModel):
    risk_percent: float = Field(default=0.5, gt=0, le=100)
    minimum_rr: float = Field(default=2.0, gt=0)
    max_risk_percent: float | None = None


class StrategyDefinition(BaseModel):
    name: str
    market: str
    timeframe: str
    direction: str | None = None  # "bullish" | "bearish"; None = either
    conditions: list[Condition] = Field(default_factory=list)  # implicit AND across the list
    entry: EntryConfig = Field(default_factory=EntryConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    score_weights: dict[str, float] | None = None
