"""Strategy engine: evaluates a StrategyDefinition against an
EvaluationContext and, if satisfied, builds the structured Signal object
described in blueprint §27. The backend — not the AI — owns this
computation; see §32 "The AI must not generate unrestricted executable
code for live trading."""

from __future__ import annotations

from dataclasses import dataclass

from app.strategy.context import EvaluationContext
from app.strategy.dsl import StrategyDefinition
from app.strategy.evaluator import evaluate_conditions
from app.strategy.scoring import compute_strategy_score

_ENTRY_BUFFER_PCT = 0.05  # small buffer beyond the zone edge for stop placement


@dataclass(slots=True)
class StrategyEvaluationResult:
    matched: bool
    satisfied: list[str]
    missing: list[str]
    direction: str | None = None
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    risk_reward: float | None = None
    score: float = 0.0


class StrategyEngine:
    def evaluate(
        self, strategy: StrategyDefinition, context: EvaluationContext
    ) -> StrategyEvaluationResult:
        all_satisfied, satisfied, missing = evaluate_conditions(strategy.conditions, context)
        if not all_satisfied:
            return StrategyEvaluationResult(matched=False, satisfied=satisfied, missing=missing)

        direction = strategy.direction or context.smc.bias
        if direction is None:
            return StrategyEvaluationResult(
                matched=False, satisfied=satisfied, missing=missing + ["direction_undetermined"]
            )

        entry, stop = self._resolve_entry_and_stop(strategy, context, direction)
        if entry is None or stop is None or entry == stop:
            return StrategyEvaluationResult(
                matched=False, satisfied=satisfied, missing=missing + ["no_valid_entry_zone"]
            )

        # Blueprint §27: a signal's stop must sit on the *losing* side of its
        # entry -- below for a long, above for a short. The default market
        # entry type anchors the stop at a dealing-range edge, and that range
        # is just the most recent confirmed swing high/low
        # (app.smc.premium_discount), which is not guaranteed to bracket the
        # current price: once price drifts through the range low, a bullish
        # setup resolves to `entry=current_price, stop=range_low` with the
        # stop *above* entry. Nothing downstream can catch that -- `RiskEngine`
        # measures the stop with `abs(entry - stop)` and `TradeRiskProposal`
        # carries no direction, so the bracket passes every risk check, and
        # the exit logic in both `app/backtest/engine.py` and
        # `app/paper/engine.py` then reads `candle.low <= stop` as a stop-loss
        # hit on the very next candle -- filling *above* the entry and booking
        # a guaranteed profit labelled `stop_loss`. That fake profit is real
        # enough to inflate `daily_pnl`, which feeds the `daily_loss_limit`
        # check and pushes the loss halt further out on exactly the day it is
        # needed most. An inverted bracket means the setup's own premise no
        # longer holds, so the correct answer is no signal at all.
        is_bullish = direction.lower() == "bullish"
        if (stop > entry) if is_bullish else (stop < entry):
            return StrategyEvaluationResult(
                matched=False, satisfied=satisfied, missing=missing + ["stop_on_wrong_side_of_entry"]
            )

        risk_per_unit = abs(entry - stop)
        minimum_rr = strategy.risk.minimum_rr
        if direction.lower() == "bullish":
            target = entry + risk_per_unit * minimum_rr
        else:
            target = entry - risk_per_unit * minimum_rr
        risk_reward = abs(target - entry) / risk_per_unit if risk_per_unit else 0.0

        satisfied_types = [c.type for c in strategy.conditions if (c.name or c.type.value) in satisfied]
        score = compute_strategy_score(
            context, satisfied_types, risk_reward, minimum_rr, weights=strategy.score_weights
        )

        return StrategyEvaluationResult(
            matched=True,
            satisfied=satisfied,
            missing=missing,
            direction=direction,
            entry=entry,
            stop=stop,
            target=target,
            risk_reward=round(risk_reward, 4),
            score=score,
        )

    @staticmethod
    def _resolve_entry_and_stop(
        strategy: StrategyDefinition, context: EvaluationContext, direction: str
    ) -> tuple[float | None, float | None]:
        smc = context.smc
        is_bullish = direction.lower() == "bullish"
        entry_type = strategy.entry.type

        if entry_type == "fvg_retest":
            gaps = smc.unmitigated_fvgs(direction=direction.upper())
            if not gaps:
                return None, None
            gap = gaps[-1]
            entry = (gap.top + gap.bottom) / 2
            buffer = gap.size * _ENTRY_BUFFER_PCT
            stop = gap.bottom - buffer if is_bullish else gap.top + buffer
            return entry, stop

        if entry_type == "order_block_retest":
            blocks = smc.active_order_blocks(direction=direction.upper())
            if not blocks:
                return None, None
            block = blocks[-1]
            entry = (block.top + block.bottom) / 2
            buffer = (block.top - block.bottom) * _ENTRY_BUFFER_PCT
            stop = block.bottom - buffer if is_bullish else block.top + buffer
            return entry, stop

        # Default: market entry at current price, stop at the nearest dealing-range edge.
        entry = context.current_price
        if smc.dealing_range is None:
            return entry, None
        stop = smc.dealing_range.range_low if is_bullish else smc.dealing_range.range_high
        return entry, stop
