"""Options payoff engine (blueprint §38): P&L at expiry for a multi-leg
strategy, correctly accounting for lot size and per-leg quantity (§38's
explicit warning)."""

from __future__ import annotations

from dataclasses import dataclass

from app.database.models.strategy import Direction
from app.options.greeks import OptionType


@dataclass(slots=True)
class OptionLeg:
    option_type: OptionType
    strike: float
    premium: float  # price per unit paid (LONG) or received (SHORT)
    quantity: float  # number of contracts, always positive
    direction: Direction  # LONG = bought, SHORT = sold
    lot_size: int = 1


@dataclass(slots=True)
class PayoffResult:
    max_profit: float | None  # None = effectively unbounded within the sampled range
    max_loss: float | None
    breakevens: list[float]
    net_premium: float  # positive = net debit paid, negative = net credit received
    capital_requirement: float


def _leg_intrinsic_value(leg: OptionLeg, underlying_price: float) -> float:
    if leg.option_type == OptionType.CALL:
        return max(underlying_price - leg.strike, 0.0)
    return max(leg.strike - underlying_price, 0.0)


def leg_payoff(leg: OptionLeg, underlying_price: float) -> float:
    intrinsic = _leg_intrinsic_value(leg, underlying_price)
    sign = 1 if leg.direction == Direction.LONG else -1
    return sign * (intrinsic - leg.premium) * leg.quantity * leg.lot_size


def total_payoff(legs: list[OptionLeg], underlying_price: float) -> float:
    return sum(leg_payoff(leg, underlying_price) for leg in legs)


def compute_payoff_curve(legs: list[OptionLeg], price_range: list[float]) -> list[tuple[float, float]]:
    return [(price, total_payoff(legs, price)) for price in price_range]


def net_premium(legs: list[OptionLeg]) -> float:
    """Positive = net debit paid to enter; negative = net credit received."""
    total = 0.0
    for leg in legs:
        sign = 1 if leg.direction == Direction.LONG else -1
        total += sign * leg.premium * leg.quantity * leg.lot_size
    return total


def compute_payoff_summary(
    legs: list[OptionLeg], price_range: list[float] | None = None
) -> PayoffResult:
    if not legs:
        raise ValueError("At least one leg is required")

    if price_range is None:
        strikes = [leg.strike for leg in legs]
        low, high = min(strikes) * 0.5, max(strikes) * 1.5
        span = high - low
        steps = 2000
        price_range = [low + span * i / steps for i in range(steps + 1)]

    curve = compute_payoff_curve(legs, price_range)
    payoffs = [p for _, p in curve]

    max_profit_sample = max(payoffs)
    max_loss_sample = min(payoffs)

    # If the payoff is still sloping at either sampled edge, the true extreme
    # in that direction is unbounded (e.g. a naked long/short call) rather
    # than whatever value happens to sit at the edge of our sample window.
    left_slope = payoffs[1] - payoffs[0]
    right_slope = payoffs[-1] - payoffs[-2]

    max_profit = max_profit_sample
    max_loss = max_loss_sample
    if (right_slope > 0 and max_profit_sample == payoffs[-1]) or (left_slope < 0 and max_profit_sample == payoffs[0]):
        max_profit = None
    if (right_slope < 0 and max_loss_sample == payoffs[-1]) or (left_slope > 0 and max_loss_sample == payoffs[0]):
        max_loss = None

    breakevens: list[float] = []
    for (p1, v1), (p2, v2) in zip(curve, curve[1:]):
        if v1 == 0:
            breakevens.append(p1)
        elif v1 * v2 < 0:
            breakevens.append(p1 + (p2 - p1) * (-v1) / (v2 - v1))

    premium = net_premium(legs)
    capital_requirement = max(premium, 0.0) or abs(min(payoffs))

    return PayoffResult(
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=breakevens,
        net_premium=premium,
        capital_requirement=capital_requirement,
    )
