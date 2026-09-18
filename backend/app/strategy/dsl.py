"""Strategy DSL (blueprint §33-34): structured, backend-validated condition
trees the AI (or a human) can produce, but never raw executable code."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


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


_BIAS_VALUES = ("bullish", "bearish")


def _validate_bias(value: str | None) -> str | None:
    """Rejects anything that is not the SMC bias vocabulary.

    `direction` was a bare `str` read as `direction.lower() == "bullish"`
    (app/strategy/engine.py, app/paper/engine.py, app/backtest/engine.py),
    so every other value -- including `""` -- silently meant *bearish*.
    That is not a hypothetical typo: this platform's own order endpoints
    take the identically-named `direction` key with the *other* vocabulary
    and enum-validate it (`Direction` LONG/SHORT in app/api/orders.py and
    app/api/options.py), so `POST /orders` refuses "bullish" while
    `POST /strategies` accepted "LONG" and traded it short. A strategy
    declared long, on a 90-120 dealing range with price at 100, evaluated
    to `entry=100 stop=120 target=60` -- stop above entry, target below --
    and the paper and autonomous engines opened a short from it.

    `app.workers.scanner_worker` already refuses to write a `Signal` for a
    direction outside this vocabulary; it was the only one of the four
    consumers that noticed the boundary existed. Rejecting at the DSL
    boundary means `POST /strategies`, `PUT /strategies/{id}` and
    `app.ai.strategy_builder.parse_strategy_json` all fail loudly instead.

    `Condition.side` and `Condition.zone` are the same shape of free text
    but fail *closed* -- an unrecognised value simply never satisfies its
    condition (app/strategy/evaluator.py), producing no trade rather than
    an inverted one -- so they are deliberately left alone here.
    """
    if value is None:
        return value
    normalized = value.strip().lower()
    if normalized not in _BIAS_VALUES:
        raise ValueError(
            f"direction={value!r} is not a valid bias. Use 'bullish' or 'bearish' -- this is the "
            "SMC bias vocabulary, not the LONG/SHORT order direction used by POST /orders."
        )
    return normalized


# Condition types whose evaluator reads `EvaluationContext.indicators`, which
# nothing writes -- see `Condition._reject_unfed_condition_types`.
_UNFED_CONDITION_TYPES = frozenset(
    {
        ConditionType.VOLUME,
        ConditionType.VOLATILITY,
        ConditionType.INDICATOR,
        ConditionType.OPTIONS_IV,
        ConditionType.OPTIONS_OI,
        ConditionType.OPTIONS_GREEKS,
    }
)


# How many bars back an event of each type still counts as "recent".
#
# These are not taste. An FVG is three candles wide and exists the moment
# it prints, so a short window is right for it. A market-structure shift is
# a *sequence*: a CHoCH breaking a confirmed swing, then a same-direction
# BOS breaking a later confirmed swing, each waiting `swing_length` bars
# for its swing to confirm (app/smc/structure.py, app/smc/swings.py). It
# cannot exist within a few bars of the sweep that caused it.
#
# One default of 5 for everything made exactly the sequence this platform
# is built around -- sweep, then structure shift, then FVG, then retest,
# the blueprint's own §34 example -- effectively unexpressible: by the time
# an MSS confirmed, the sweep that caused it had aged out of its window.
# Measured over 3120 bar-evaluations of 60 random walks, counting bars
# where the §34 conjunction holds:
#
#     lookback   sweep+mss   all three
#            5           1           1
#           10           9           7
#           20          62          45
#           40         376         288
#           80         996         804
#
# A strategy that fires on 0.03% of bars is not a strategy. 30 puts the
# multi-bar structure events in the range where the sequence they describe
# can actually complete, while leaving single-print events short.
_DEFAULT_LOOKBACK_BY_TYPE: dict[ConditionType, int] = {
    ConditionType.MSS: 30,
    ConditionType.CHOCH: 30,
    ConditionType.BOS: 30,
    ConditionType.LIQUIDITY_SWEEP: 30,
    ConditionType.ORDER_BLOCK: 20,
}
_DEFAULT_LOOKBACK = 5


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
    # How many recent candles count as "recent" for event-type conditions.
    # Left unset, it is filled per condition type by
    # `_default_lookback_per_condition_type` below -- a single scalar cannot
    # serve every type, because the events differ by an order of magnitude
    # in how long they take to form.
    lookback: int | None = None

    @model_validator(mode="after")
    def _default_lookback_per_condition_type(self) -> "Condition":
        """Fills `lookback` from the event's own formation time when the
        author did not state one.

        Kept as a post-validation fill rather than a plain field default so
        the evaluator never sees `None` and no caller has to know the
        table. An explicit `lookback` is always honoured, including one
        that is shorter than the default.
        """
        if self.lookback is None:
            self.lookback = _DEFAULT_LOOKBACK_BY_TYPE.get(self.type, _DEFAULT_LOOKBACK)
        return self

    @model_validator(mode="after")
    def _reject_a_lookback_that_can_never_match(self) -> "Condition":
        """Rejects `lookback` below 1, which makes the condition false on
        every candle forever.

        `evaluate_condition` asks `context.current_index - event.index <
        condition.lookback` for `bos`, `mss`, `choch` and
        `liquidity_sweep`. On the very bar the event happened that
        difference is 0, so `lookback=1` is the smallest value that can
        ever be true, and it means "only on the event bar". At 0 and
        below nothing satisfies it -- not even the bar the structure
        break printed on.

        Measured through `StrategyEngine.evaluate` with a real BOS at
        index 7, evaluated one bar later:

            lookback=2   -> satisfied=['bos']
            lookback=1   -> missing=['bos']     (correct: the event is a bar old)
            lookback=0   -> missing=['bos']
            lookback=-5  -> missing=['bos']

        and `POST /strategies` answered 201 for the `lookback=0` version.

        Accepted silently, that is the same trap as the two validators
        around it. Conditions AND implicitly (`evaluate_conditions`), so
        one of them zeroes the whole strategy: it stores, lists,
        backtests and auto-trades like any other and simply never
        produces a signal, and a validation backtest reporting zero
        trades is indistinguishable from "this history had no setups".

        No upper bound. A very large `lookback` means "this event never
        expires", which is a defensible authoring choice -- it is what
        every structure condition did before the expiry window existed --
        and `lookback` lives inside a JSON column, so there is no width
        to overflow. Only the end that cannot match is refused.

        Compares the value directly rather than guarding for `None`:
        `_default_lookback_per_condition_type` is declared above this and
        pydantic runs `mode="after"` validators in declaration order, so
        the field always holds an int by the time this runs. A `None`
        guard here would be unreachable -- injecting one changed nothing
        and the suite stayed green, which is how it was found. If that
        fill-in is ever removed, this raises a `TypeError` naming the
        line instead of quietly admitting `None`, and
        `test_an_omitted_lookback_still_gets_its_per_type_default` fails.
        """
        if self.lookback < 1:
            raise ValueError(
                f"lookback={self.lookback} can never match: an event on the current candle is "
                "0 bars old, so the smallest useful value is 1 (meaning 'only on the bar the "
                "event printed'). Because conditions AND together, this would stop the whole "
                "strategy from ever firing."
            )
        return self

    @model_validator(mode="after")
    def _reject_premium_discount_without_a_zone(self) -> "Condition":
        """Rejects `premium_discount` with no `zone`, which can never match.

        Every other condition type reads an omitted optional as "any": an
        `fvg` with no direction matches a gap either way, an
        `order_block` likewise, a `session` with no name matches any kill
        zone. `premium_discount` is the exception -- its evaluator arm ends
        in `condition.zone is not None and ...`, so leaving the field out
        makes the condition false on every candle forever. Measured with a
        live dealing range and `current_zone='PREMIUM'`: the zone-less
        condition returns False while `zone='premium'` returns True.

        Accepted silently, that is worse than it sounds. Conditions AND
        implicitly (`evaluate_conditions`), so one of them zeroes the whole
        strategy -- the strategy stores, lists and backtests like any
        other, and simply never produces a signal. This is the same ruling
        `_reject_unfed_condition_types` above makes for the condition types
        nothing feeds, and `_reject_unknown_entry_types` makes for
        `entry.type`: fail at authoring time rather than look alive and do
        nothing.

        Deliberately *not* extended to an unrecognised zone string. Those
        fail closed too, but `_validate_bias`'s docstring records leaving
        free-text `side`/`zone` values alone as an intentional choice, and
        widening it here would overturn that decision rather than fill the
        gap this validator is for: a value the author never supplied at
        all.
        """
        if self.type is ConditionType.PREMIUM_DISCOUNT and not (self.zone or "").strip():
            raise ValueError(
                "a premium_discount condition needs a zone (e.g. zone='premium' or "
                "zone='discount'); without one it can never match, and because "
                "conditions AND together it would stop the whole strategy from ever firing"
            )
        return self

    @field_validator("type")
    @classmethod
    def _reject_unfed_condition_types(cls, v: ConditionType) -> ConditionType:
        """Rejects the condition types no data source feeds.

        `app.strategy.evaluator` routes all six of these to
        `_numeric_compare(context.indicators.get(key), condition)`, and
        `EvaluationContext.indicators` has no writer anywhere in `app/` --
        all five construction sites (`app/backtest/engine.py`,
        `app/paper/engine.py`, `app/api/ai.py`, `app/api/scanner.py`,
        `app/workers/scanner_worker.py`) omit it, so the bag is always
        empty, `.get` always returns `None`, and `_numeric_compare` returns
        `False` on its first line. Every such condition is therefore false
        on every candle forever.

        Because conditions AND implicitly, one of them zeroes the whole
        strategy: a working `fvg` strategy stops producing signals the
        moment a *vacuously true* `volume > 0` is added to it, against bars
        that carry volume. Nothing surfaces why -- `POST /backtest` runs the
        same evaluator over the same context shape, so a validation
        backtest reports zero trades, indistinguishable from "this history
        had no setups", and blueprint §77's graduation path cannot catch it
        at any stage.

        This is the same ruling `_reject_unimplemented_boolean_operators`
        below already makes for AND/OR/NOT, and `_reject_unknown_entry_types`
        makes for `entry.type` -- and the worked example in that fix's
        write-up (docs/ARCHITECTURE.md) is itself an `indicator` condition,
        rejected only for its operator. Failing loudly at authoring time
        beats a strategy that looks alive and is not.

        `volume` and `volatility` could be computed from the candle series
        each caller already holds; `indicator` needs a library; `options_*`
        additionally needs the option-chain ingestion the docs record as
        missing. Populating `indicators` at those five sites is the other
        way to close this, and is what should replace this validator when
        the data exists -- rejecting is not a claim that these types are
        unwanted, only that accepting them today is dishonest.
        """
        if v in _UNFED_CONDITION_TYPES:
            raise ValueError(
                f"condition type={v.value!r} is declared in the schema but nothing populates the data it "
                "reads (EvaluationContext.indicators has no writer), so a condition using it would "
                "silently never match on any candle -- and because conditions AND together, it would stop "
                "the whole strategy from ever firing. Use the structural condition types "
                f"({', '.join(t.value for t in ConditionType if t not in _UNFED_CONDITION_TYPES)}) instead."
            )
        return v

    @field_validator("direction")
    @classmethod
    def _validate_direction(cls, v: str | None) -> str | None:
        return _validate_bias(v)

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


_ENTRY_TYPES = ("market", "fvg_retest", "order_block_retest")


class EntryConfig(BaseModel):
    """How a matched setup turns into a price to enter at.

    `params` is accepted and persisted but not read by any entry type yet
    -- `app.strategy.engine._resolve_entry_and_stop` derives every entry
    and stop from the zone geometry alone. It is left in place rather than
    removed because the DSL is a stored, versioned document; nothing is
    silently *mis*-interpreted by it, unlike `type` below.
    """

    type: str = "market"
    params: dict = Field(default_factory=dict)

    @field_validator("type")
    @classmethod
    def _reject_unknown_entry_types(cls, v: str) -> str:
        """Rejects anything outside the three entry types that exist.

        `type` was a bare `str` read as a chain of bare equality tests
        with an implicit `else` (`app/strategy/engine.py`'s
        `_resolve_entry_and_stop`): `== "fvg_retest"`, then
        `== "order_block_retest"`, then fall through to a market entry at
        `context.current_price` with the stop at the dealing-range edge.
        So every value that was not exactly one of those two strings --
        `"FVG_RETEST"`, `"fvg-retest"`, `"limit"`, a typo, an AI
        hallucination -- silently became a *market* entry, which is not a
        near miss but a different trade:

            entry.type='fvg_retest'  entry=102.75 stop=99.17 target=109.90
                                     stop 3.48% away, fills only once price
                                     trades back down to the gap
            entry.type='FVG_RETEST'  entry=105.00 stop=96.00 target=123.00
                                     stop 8.57% away, fills immediately

        Same candles, same conditions, one character of case. The
        substitute chases price at the top of the move instead of waiting
        for the retest, brackets itself against a level 2.5x further away,
        and -- because the retest gates in `app/paper/engine.py` and
        `app/backtest/engine.py` only fill when `low <= entry <= high` --
        takes a position on a candle where the strategy as written would
        have taken none at all. A backtest run to validate the strategy
        silently exercises the substitute too, so it never reveals the
        swap.

        This is the third field in this DSL to fail this way, and the
        third named in `app.ai.strategy_builder`'s own system prompt
        ("Only use condition types, operators, and entry types the schema
        defines") -- `Condition.operator` and `direction` are already
        rejected above. Unlike `Condition.side`/`zone`, which fail
        *closed* (an unrecognised value simply never satisfies its
        condition), this one fails open into a live trade, which is why it
        cannot be left alone.

        Case and surrounding whitespace are normalised rather than
        rejected, matching `_validate_bias`.
        """
        normalized = v.strip().lower()
        if normalized not in _ENTRY_TYPES:
            raise ValueError(
                f"entry.type={v!r} is not a supported entry type. Use one of "
                f"{', '.join(_ENTRY_TYPES)} -- an unrecognised value used to be silently "
                "traded as a market entry at the current price, which is a different trade."
            )
        return normalized


# `strategies.definition` is a JSON column, and Postgres' JSON type has no
# `Infinity`: `minimum_rr: Infinity` reached it as the bare token and came
# back as `InvalidTextRepresentationError` -- a 500 on POST /strategies for
# a value pydantic was happy to accept, since `gt=0` passes `inf`. The
# ceiling is the one `app/api/orders.py` and `app/api/replay.py` already
# use, for the same reason: every number this DSL produces ends up in a
# `Numeric(18, 6)` column somewhere downstream.
_MAX_MINIMUM_RR = 1e12


class RiskConfig(BaseModel):
    risk_percent: float = Field(default=0.5, gt=0, le=100)
    # Bounding the field is NOT the whole fix, and that is the point worth
    # remembering here: `app/strategy/engine.py` computes
    # `target = entry +/- risk_per_unit * minimum_rr`, so what overflows
    # `positions.target` is a *product*. A `minimum_rr` well inside this
    # ceiling still overflows against a large enough `risk_per_unit`, which
    # is why the engine guards the product where it is formed. Measured
    # before either guard: `minimum_rr=1e308` stored happily, and then the
    # paper session 500'd with NumericValueOutOfRangeError on the bar the
    # strategy first entered on -- with nothing journalled for that candle.
    minimum_rr: float = Field(default=2.0, gt=0, le=_MAX_MINIMUM_RR)
    max_risk_percent: float | None = None


# `strategies.name` and `strategy_versions.name` are both `String(255)`;
# a longer name reached Postgres and came back as a 500 rather than a 422.
_MAX_NAME = 255
# `candles.timeframe` is `String(8)`, so no candle row can carry a longer
# timeframe -- 8 characters stores, 9 raises `StringDataRightTruncation`.
# Every consumer of this field (`ScannerWorker`, `AutoTradeSupervisor`,
# replay and backtest) loads candles by this exact string, so a strategy
# whose timeframe cannot fit that column can never be evaluated by any of
# them: it is accepted, stored, and silently never fires. Same rule as the
# entry- and condition-type validators below -- the DSL does not accept a
# strategy the engine can never satisfy.
_MAX_TIMEFRAME = 8


class StrategyDefinition(BaseModel):
    name: str = Field(min_length=1, max_length=_MAX_NAME)
    # `market` is deliberately unbounded: it has no reader anywhere in
    # `app/` and lands only in the JSON `definition` column, so there is no
    # width to match it to and no failure to prevent.
    market: str
    timeframe: str = Field(min_length=1, max_length=_MAX_TIMEFRAME)
    direction: str | None = None  # "bullish" | "bearish"; None = either
    conditions: list[Condition] = Field(default_factory=list)  # implicit AND across the list
    entry: EntryConfig = Field(default_factory=EntryConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    score_weights: dict[str, float] | None = None

    @field_validator("direction")
    @classmethod
    def _validate_direction(cls, v: str | None) -> str | None:
        return _validate_bias(v)
