from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.auth.dependencies import get_current_user
from app.database.models.users import User
from app.options.greeks import OptionType, black_scholes_greeks, black_scholes_price
from app.options.payoff import compute_payoff_summary
from app.options.strategies import BIAS_STRATEGIES, build_strategy

router = APIRouter(prefix="/options", tags=["options"])


class GreeksRequest(BaseModel):
    spot: float
    strike: float
    time_to_expiry_years: float
    rate: float = 0.06
    iv: float
    option_type: OptionType


class GreeksResponse(BaseModel):
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float
    rho: float


@router.post("/greeks", response_model=GreeksResponse)
async def compute_greeks(payload: GreeksRequest, user: User = Depends(get_current_user)) -> GreeksResponse:
    try:
        price = black_scholes_price(
            payload.spot, payload.strike, payload.time_to_expiry_years, payload.rate, payload.iv, payload.option_type
        )
        greeks = black_scholes_greeks(
            payload.spot, payload.strike, payload.time_to_expiry_years, payload.rate, payload.iv, payload.option_type
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return GreeksResponse(price=price, **greeks.__dict__)


class StrategyLegInput(BaseModel):
    strike: float
    premium_call: float | None = None
    premium_put: float | None = None


class BuildStrategyRequest(BaseModel):
    strategy_name: str
    legs_by_strike: dict[float, StrategyLegInput]
    quantity: float = 1
    lot_size: int = 1
    strategy_kwargs: dict = {}


class LegResponse(BaseModel):
    option_type: str
    strike: float
    premium: float
    quantity: float
    direction: str


class PayoffResponse(BaseModel):
    legs: list[LegResponse]
    max_profit: float | None
    max_loss: float | None
    breakevens: list[float]
    net_premium: float
    capital_requirement: float


@router.get("/strategies")
async def list_available_strategies(user: User = Depends(get_current_user)) -> dict:
    return {"by_bias": BIAS_STRATEGIES}


@router.post("/strategy", response_model=PayoffResponse)
async def build_option_strategy(payload: BuildStrategyRequest, user: User = Depends(get_current_user)) -> PayoffResponse:
    chain = {
        strike: {"CALL": leg.premium_call, "PUT": leg.premium_put}
        for strike, leg in payload.legs_by_strike.items()
    }
    try:
        legs = build_strategy(
            payload.strategy_name, chain, quantity=payload.quantity, lot_size=payload.lot_size, **payload.strategy_kwargs
        )
        summary = compute_payoff_summary(legs)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    return PayoffResponse(
        legs=[
            LegResponse(
                option_type=leg.option_type.value,
                strike=leg.strike,
                premium=leg.premium,
                quantity=leg.quantity,
                direction=leg.direction.value,
            )
            for leg in legs
        ],
        max_profit=summary.max_profit,
        max_loss=summary.max_loss,
        breakevens=summary.breakevens,
        net_premium=summary.net_premium,
        capital_requirement=summary.capital_requirement,
    )
