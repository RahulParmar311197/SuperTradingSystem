"""Strategy DSL (blueprint §33-34): structured, backend-validated condition
trees the AI (or a human) can produce, but never raw executable code."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


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
