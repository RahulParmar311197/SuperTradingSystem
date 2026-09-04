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
    indicators: dict[str, float] = field(default_factory=dict)
    session_tags: set[str] = field(default_factory=set)
