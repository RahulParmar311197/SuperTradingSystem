import dataclasses
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.orders import _execution_mode_for, _stack_for
from app.auth.dependencies import get_current_user, require_permission
from app.brokers.mock import MockBroker
from app.core.audit import record_audit
from app.core.redis import account_halt_reason
from app.database.models.instruments import Instrument
from app.database.models.instruments import MarketType as InstrumentMarketType
from app.database.models.notifications import NotificationType
from app.database.models.options import OptionContract, OptionSnapshot
from app.database.models.risk import RiskDecision as RiskEventDecision
from app.database.models.risk import RiskEvent
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.database.models.users import TradingPermission, User
from app.database.session import get_db
from app.notifications.service import create_notification
from app.options.greeks import OptionType, black_scholes_greeks, black_scholes_price
from app.options.liquidity_filter import evaluate_liquidity
from app.options.payoff import OptionLeg, compute_payoff_summary
from app.options.strategies import BIAS_STRATEGIES, build_strategy
from app.risk.kill_switch import load_kill_switch_state
from app.risk.options_risk import OptionsRiskProposal, evaluate_options_risk
from app.trading.persistence import persist_order, persist_position, record_trade

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
    # Greeks is `@dataclass(slots=True)` -- no `__dict__` attribute; this
    # endpoint had never had a test hit it, so it 500'd on every call.
    return GreeksResponse(price=price, **dataclasses.asdict(greeks))


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


class ExecuteOptionLegRequest(BaseModel):
    symbol: str
    direction: Direction
    quantity: float  # number of lots
    # Current market price per unit for this leg — no live options feed
    # exists in this environment (see docs/ARCHITECTURE.md), so this
    # mirrors POST /orders's `entry` field: MockBroker is fed this price
    # directly; a real broker ignores it and prices its own fill.
    premium: float


class ExecuteOptionsStrategyRequest(BaseModel):
    strategy_name: str
    legs: list[ExecuteOptionLegRequest]


class LegExecutionResult(BaseModel):
    symbol: str
    order_id: uuid.UUID
    status: str
    rejection_reason: str | None


class ExecuteOptionsStrategyResponse(BaseModel):
    batch_id: uuid.UUID
    max_profit: float | None
    max_loss: float | None
    net_premium: float
    capital_requirement: float
    liquidity_warnings: list[str]
    legs: list[LegExecutionResult]


async def _latest_option_snapshot(db: AsyncSession, instrument_id: uuid.UUID) -> OptionSnapshot | None:
    contract = (
        await db.execute(select(OptionContract).where(OptionContract.instrument_id == instrument_id))
    ).scalar_one_or_none()
    if contract is None:
        return None
    return (
        await db.execute(
            select(OptionSnapshot)
            .where(OptionSnapshot.option_contract_id == contract.id)
            .order_by(OptionSnapshot.snapshot_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


@router.post("/execute", response_model=ExecuteOptionsStrategyResponse, status_code=status.HTTP_201_CREATED)
async def execute_options_strategy(
    payload: ExecuteOptionsStrategyRequest,
    user: User = Depends(require_permission(TradingPermission.LIVE_TRADE)),
    db: AsyncSession = Depends(get_db),
) -> ExecuteOptionsStrategyResponse:
    """Submits every leg of a multi-leg options strategy (blueprint §37,
    §120) as real orders through the same broker/risk/persistence
    pipeline `POST /orders` uses — gated once, together, by the
    strategy's combined payoff (blueprint §38-40) rather than the
    single-trade entry/stop shape `POST /orders` uses for a directional
    trade, which doesn't apply to a defined-risk combination.

    Each leg is still a *separate* order once submitted — neither this
    codebase nor (as far as it's been verified) Upstox/Dhan guarantee
    exchange-level atomic multi-leg fills, so a later leg's rejection
    does not undo an earlier leg's fill. Every leg's own outcome is
    reported in the response; nothing here pretends this is atomic.
    """
    if not payload.legs:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "At least one leg is required")

    halt_reason = await account_halt_reason(str(user.id))
    if halt_reason is not None:
        raise HTTPException(status.HTTP_423_LOCKED, f"New entries are halted for this account: {halt_reason}")

    instruments: dict[str, Instrument] = {}
    for leg in payload.legs:
        instrument = (
            await db.execute(select(Instrument).where(Instrument.symbol == leg.symbol, Instrument.active.is_(True)))
        ).scalar_one_or_none()
        if instrument is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown instrument symbol: {leg.symbol}")
        if instrument.market != InstrumentMarketType.OPTIONS or instrument.option_type is None or instrument.strike is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"{leg.symbol} is not an options contract")
        instruments[leg.symbol] = instrument

    option_legs = [
        OptionLeg(
            option_type=OptionType(instruments[leg.symbol].option_type.value),
            strike=float(instruments[leg.symbol].strike),
            premium=leg.premium,
            quantity=leg.quantity,
            direction=leg.direction,
            lot_size=instruments[leg.symbol].lot_size,
        )
        for leg in payload.legs
    ]
    try:
        payoff = compute_payoff_summary(option_legs)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    # Liquidity (blueprint §40): a leg with no snapshot data at all only
    # produces a warning — this environment has no options-chain
    # ingestion pipeline yet (see docs/ARCHITECTURE.md), so treating
    # "never populated" the same as "actually illiquid" would make this
    # endpoint permanently unusable rather than honestly degraded.
    liquidity_warnings: list[str] = []
    liquidity_acceptable = True
    # Worst-case deviation across legs, not summed -- one leg's claimed
    # premium being wildly off from the real market is already enough
    # reason to reject the whole strategy (blueprint §56/§57's
    # "entry_matches_market" precedent, extended here to options premiums:
    # see RiskLimits.max_premium_deviation_pct).
    premium_deviation_pct = 0.0
    # Worst (max) staleness across every leg with a real OptionSnapshot --
    # 0.0 when no leg has snapshot data yet, matching premium_deviation_pct's
    # same "nothing to check yet" default just above.
    market_data_age_seconds = 0.0
    for leg in payload.legs:
        snapshot = await _latest_option_snapshot(db, instruments[leg.symbol].id)
        if snapshot is None:
            liquidity_warnings.append(f"{leg.symbol}: no liquidity data available — not evaluated")
            continue
        assessment = evaluate_liquidity(
            volume=float(snapshot.volume),
            open_interest=float(snapshot.open_interest),
            bid=float(snapshot.bid) if snapshot.bid is not None else None,
            ask=float(snapshot.ask) if snapshot.ask is not None else None,
            quote_timestamp=snapshot.snapshot_at,
        )
        liquidity_warnings.extend(f"{leg.symbol}: {w}" for w in assessment.warnings)
        if not assessment.acceptable:
            liquidity_acceptable = False
            liquidity_warnings.extend(f"{leg.symbol}: {r}" for r in assessment.rejections)
        if snapshot.bid is not None and snapshot.ask is not None:
            mid = (float(snapshot.bid) + float(snapshot.ask)) / 2
            if mid:
                deviation = abs(leg.premium - mid) / mid * 100
                premium_deviation_pct = max(premium_deviation_pct, deviation)
        age = (datetime.now(timezone.utc) - snapshot.snapshot_at).total_seconds()
        market_data_age_seconds = max(market_data_age_seconds, age)

    stack = await _stack_for(user, db)
    open_positions = stack.position_manager.open_positions(str(user.id))
    current_exposure = sum(abs(p.quantity) * p.average_price for p in open_positions)

    risk_proposal = OptionsRiskProposal(
        account_id=str(user.id),
        account_balance=(await stack.broker.get_account()).balance,
        current_exposure=current_exposure,
        payoff=payoff,
        broker_healthy=await stack.broker.is_healthy(),
        market_data_age_seconds=market_data_age_seconds,
        liquidity_acceptable=liquidity_acceptable,
        premium_deviation_pct=premium_deviation_pct,
    )
    # Blueprint §58: fetched fresh on every call so a kill triggered via the
    # admin endpoint from this or any other process takes effect on the
    # very next order -- see app.risk.kill_switch.load_kill_switch_state.
    kill_switch = await load_kill_switch_state(str(user.id), None)
    decision = evaluate_options_risk(risk_proposal, limits=stack.risk_engine.limits, kill_switch=kill_switch)
    db.add(
        RiskEvent(
            user_id=user.id,
            decision=RiskEventDecision.APPROVE if decision.approved else RiskEventDecision.REJECT,
            reason=decision.reason,
            checks={c.name: c.passed for c in decision.checks},
        )
    )
    await db.commit()

    if not decision.approved:
        # Blueprint §63 mandates an "Order rejected" notification -- the
        # equity path (POST /orders, app/api/orders.py) already fires one
        # and writes this same RiskEvent audit row on rejection; this
        # endpoint places real orders through that same broker/risk/
        # persistence pipeline (see this function's own docstring) but
        # used to only raise the HTTPException below, with no RiskEvent
        # row and no notification either -- nothing else in the system
        # (another device, GET /notifications, an admin view) ever
        # learned an options strategy was blocked, and no audit trail
        # existed for *any* options risk decision, approved or not.
        await create_notification(
            db,
            user_id=user.id,
            notification_type=NotificationType.ORDER_REJECTED,
            title=f"{payload.strategy_name} options strategy rejected",
            body=decision.reason or "Risk engine rejected this strategy",
            data={"strategy_name": payload.strategy_name, "reason": decision.reason},
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Risk engine rejected this strategy: {decision.reason}")

    batch_id = uuid.uuid4()
    execution_mode = _execution_mode_for(stack)
    leg_results: list[LegExecutionResult] = []
    for leg in payload.legs:
        instrument = instruments[leg.symbol]
        if isinstance(stack.broker, MockBroker):
            stack.broker.set_quote(leg.symbol, ltp=leg.premium)

        existing_position = stack.position_manager.get(str(user.id), leg.symbol)
        realized_pnl_before = existing_position.realized_pnl if existing_position is not None else 0.0
        position_before = (
            {
                "is_long": existing_position.is_long,
                "average_price": existing_position.average_price,
                "quantity": existing_position.quantity,
            }
            if existing_position is not None
            else None
        )

        idempotency_key = f"{user.id}:{batch_id}:{leg.symbol}"
        total_quantity = leg.quantity * instrument.lot_size
        order, created = stack.order_manager.create_order(
            idempotency_key, str(user.id), leg.symbol, leg.direction, OrderType.MARKET, total_quantity
        )
        if created:
            stack.order_manager.transition(order.id, OrderStatus.VALIDATING)
            stack.order_manager.transition(order.id, OrderStatus.RISK_APPROVED, f"options strategy batch {batch_id}")
            await stack.execution_engine.submit(order.id)

        final_order = stack.order_manager.get(order.id)
        await persist_order(
            db, final_order, user.id, instrument.id, execution_mode=execution_mode, broker_account_id=stack.broker_account_id
        )

        position_after = stack.position_manager.get(str(user.id), leg.symbol)
        if position_after is not None:
            # Same "did a real fill actually happen" guard as
            # app/api/orders.py's place_order -- a broker-rejected leg
            # leaves `position_after` reflecting whatever existed before
            # this call, unchanged.
            just_filled = created and final_order.status in (
                OrderStatus.FILLED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.MONITORING,
            )
            opened_or_added = (
                just_filled and position_after.is_open and position_after.is_long == (leg.direction == Direction.LONG)
            )
            position_row = await persist_position(db, user.id, instrument.id, position_after, execution_mode=execution_mode)
            realized_delta = position_after.realized_pnl - realized_pnl_before
            if realized_delta != 0 and position_before is not None:
                await record_trade(
                    db,
                    user_id=user.id,
                    instrument_id=instrument.id,
                    direction=Direction.LONG if position_before["is_long"] else Direction.SHORT,
                    entry_price=position_before["average_price"],
                    # Same fix as app/api/orders.py's record_trade call --
                    # the real broker fill price, not the client-supplied
                    # `leg.premium`, which `pnl` above was actually computed
                    # from (via PositionManager.apply_fill).
                    exit_price=final_order.average_fill_price,
                    quantity=abs(position_before["quantity"]),
                    pnl=realized_delta,
                    position_id=position_row.id,
                    execution_mode=execution_mode,
                )
                # Blueprint §63/§104 parity with app/api/orders.py's
                # identical fix -- this endpoint places real orders
                # through the same broker/risk/persistence pipeline (its
                # own docstring says so) but used to only notify on
                # rejection, never on an actual closing fill.
                await create_notification(
                    db,
                    user_id=user.id,
                    notification_type=NotificationType.POSITION_CLOSED,
                    title=f"{leg.symbol} position closed",
                    body=f"Realized P&L: {realized_delta:.2f}",
                    data={"symbol": leg.symbol, "pnl": realized_delta},
                )
            if opened_or_added:
                await create_notification(
                    db,
                    user_id=user.id,
                    notification_type=NotificationType.TRADE_EXECUTED,
                    title=f"{leg.symbol} order executed",
                    body=f"Opened {leg.direction.value} position in {leg.symbol}",
                    data={"symbol": leg.symbol, "direction": leg.direction.value, "strategy_name": payload.strategy_name},
                )

        await record_audit(
            db,
            actor="user",
            action="options_strategy.leg_placed",
            user_id=user.id,
            details={
                "batch_id": str(batch_id),
                "strategy_name": payload.strategy_name,
                "symbol": leg.symbol,
                "direction": leg.direction.value,
                "status": final_order.status.value,
            },
        )
        leg_results.append(
            LegExecutionResult(
                symbol=leg.symbol, order_id=final_order.id, status=final_order.status.value, rejection_reason=final_order.rejection_reason
            )
        )

    return ExecuteOptionsStrategyResponse(
        batch_id=batch_id,
        max_profit=payoff.max_profit,
        max_loss=payoff.max_loss,
        net_premium=payoff.net_premium,
        capital_requirement=payoff.capital_requirement,
        liquidity_warnings=liquidity_warnings,
        legs=leg_results,
    )
