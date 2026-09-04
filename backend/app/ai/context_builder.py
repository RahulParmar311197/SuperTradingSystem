"""Builds the structured facts handed to the AI (blueprint §30, §80).

The AI receives pre-computed structure — it never sees raw candle arrays
and is never asked to compute SMC/ICT concepts itself (§79: "always provide
the structured facts used by the AI").
"""

from __future__ import annotations

from app.strategy.context import EvaluationContext


def build_ai_prompt_context(context: EvaluationContext) -> dict:
    smc = context.smc
    ict = context.ict

    latest_structure = smc.structure_events[-1] if smc.structure_events else None
    latest_mss = smc.mss_events[-1] if smc.mss_events else None
    sweeps = smc.recent_sweeps()

    return {
        "symbol": context.symbol,
        "timeframe": context.timeframe,
        "timestamp": context.timestamp.isoformat(),
        "price": context.current_price,
        "htf_bias": smc.bias,
        "structure": {
            "latest_event": latest_structure.event_type.value if latest_structure else None,
            "latest_direction": latest_structure.direction.value if latest_structure else None,
            "mss_confirmed": latest_mss is not None,
        },
        "liquidity": {
            "recent_sweeps": [
                {"side": p.side.value, "source": p.source_type.value, "price": p.price, "rejected": p.rejected}
                for p in sweeps
            ]
        },
        "fair_value_gaps": [
            {"direction": g.direction.value, "top": g.top, "bottom": g.bottom, "filled_pct": g.filled_percentage}
            for g in smc.unmitigated_fvgs()
        ],
        "order_blocks": [
            {"direction": b.direction.value, "top": b.top, "bottom": b.bottom, "strength": b.strength}
            for b in smc.active_order_blocks()
        ],
        "premium_discount": {
            "zone": smc.current_zone,
            "range_high": smc.dealing_range.range_high if smc.dealing_range else None,
            "range_low": smc.dealing_range.range_low if smc.dealing_range else None,
        },
        "kill_zones": ict.current_kill_zones,
        "indicators": dict(context.indicators),
    }
