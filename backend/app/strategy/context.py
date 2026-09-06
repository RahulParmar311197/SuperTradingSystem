"""The evaluation context bundles everything a strategy condition might
need: SMC/ICT structure, and a generic bag of numeric/text indicator values
(volume, volatility, session tag, options Greeks/IV/OI) so the DSL stays
extensible without the evaluator needing to know about every data source."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app.ict.engine import ICTContext
from app.smc.engine import SMCContext


@dataclass(slots=True)
class EvaluationContext:
    symbol: str
    timeframe: str
    timestamp: datetime
    current_price: float
    smc: SMCContext
    ict: ICTContext
    # Index of the current (most recent) candle within the same candle list
    # `smc`/`ict` were computed from -- `StructureEvent.index` and
    # `LiquidityPool.swept_index` (app.smc.types) are indices into that
    # identical list, so `current_index - event.index` is how
    # `app.strategy.evaluator` measures "how many candles ago" an
    # event-type condition's match actually happened, against
    # `Condition.lookback`. Defaults to 0 so a caller that never sets it
    # (there shouldn't be one among real strategy-evaluation call sites)
    # falls back to the pre-lookback-filtering behavior rather than
    # wrongly rejecting a genuinely recent event.
    current_index: int = 0
    indicators: dict[str, float] = field(default_factory=dict)
    session_tags: set[str] = field(default_factory=set)
