"""Premium/discount dealing-range zones (blueprint §25)."""

from __future__ import annotations

from app.smc.types import PremiumDiscountZone, Swing


def dealing_range_from_swings(swings: list[Swing]) -> PremiumDiscountZone | None:
    """Uses the most recent visible swing high and swing low as the dealing
    range boundaries. Returns None until both exist."""
    highs = [s for s in swings if s.swing_type.value == "HIGH"]
    lows = [s for s in swings if s.swing_type.value == "LOW"]
    if not highs or not lows:
        return None
    return PremiumDiscountZone(range_high=highs[-1].price, range_low=lows[-1].price)
