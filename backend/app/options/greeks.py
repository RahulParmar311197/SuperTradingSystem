"""Black-Scholes pricing and Greeks (blueprint §39)."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class OptionType(StrEnum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass(slots=True)
class Greeks:
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _d1_d2(spot: float, strike: float, time_to_expiry: float, rate: float, iv: float) -> tuple[float, float]:
    if time_to_expiry <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        raise ValueError("spot, strike, time_to_expiry and iv must all be positive")
    d1 = (math.log(spot / strike) + (rate + 0.5 * iv**2) * time_to_expiry) / (iv * math.sqrt(time_to_expiry))
    d2 = d1 - iv * math.sqrt(time_to_expiry)
    return d1, d2


def black_scholes_price(
    spot: float, strike: float, time_to_expiry: float, rate: float, iv: float, option_type: OptionType
) -> float:
    d1, d2 = _d1_d2(spot, strike, time_to_expiry, rate, iv)
    if option_type == OptionType.CALL:
        return spot * _norm_cdf(d1) - strike * math.exp(-rate * time_to_expiry) * _norm_cdf(d2)
    return strike * math.exp(-rate * time_to_expiry) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def black_scholes_greeks(
    spot: float, strike: float, time_to_expiry: float, rate: float, iv: float, option_type: OptionType
) -> Greeks:
    d1, d2 = _d1_d2(spot, strike, time_to_expiry, rate, iv)
    pdf_d1 = _norm_pdf(d1)
    sqrt_t = math.sqrt(time_to_expiry)

    gamma = pdf_d1 / (spot * iv * sqrt_t)
    vega = spot * pdf_d1 * sqrt_t / 100  # per 1 vol point (1%)

    if option_type == OptionType.CALL:
        delta = _norm_cdf(d1)
        theta = (
            -spot * pdf_d1 * iv / (2 * sqrt_t) - rate * strike * math.exp(-rate * time_to_expiry) * _norm_cdf(d2)
        ) / 365
        rho = strike * time_to_expiry * math.exp(-rate * time_to_expiry) * _norm_cdf(d2) / 100
    else:
        delta = _norm_cdf(d1) - 1
        theta = (
            -spot * pdf_d1 * iv / (2 * sqrt_t) + rate * strike * math.exp(-rate * time_to_expiry) * _norm_cdf(-d2)
        ) / 365
        rho = -strike * time_to_expiry * math.exp(-rate * time_to_expiry) * _norm_cdf(-d2) / 100

    return Greeks(delta=delta, gamma=gamma, theta=theta, vega=vega, rho=rho)
