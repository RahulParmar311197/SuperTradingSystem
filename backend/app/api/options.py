import dataclasses
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.orders import _execution_mode_for, _stack_for
from app.auth.dependencies import get_current_user, require_permission
from app.brokers.mock import MockBroker
from app.core.audit import record_audit
from app.core.redis import account_halt_reason, halt_account
from app.database.models.instruments import Instrument
from app.database.models.instruments import MarketType as InstrumentMarketType
from app.database.models.notifications import NotificationType
from app.database.models.options import OptionContract, OptionSnapshot
from app.database.models.risk import RiskDecision as RiskEventDecision
from app.database.models.risk import RiskEvent
from app.database.models.strategy import Direction
from app.database.models.trading import ExecutionMode, OrderStatus, OrderType
from app.database.models.users import TradingPermission, User
from app.database.session import get_db
from app.notifications.service import create_notification
from app.options.greeks import OptionType, black_scholes_greeks, black_scholes_price
from app.options.liquidity_filter import evaluate_liquidity
from app.options.payoff import OptionLeg, compute_payoff_summary
from app.options.strategies import BIAS_STRATEGIES, build_strategy
from app.risk.kill_switch import load_kill_switch_state
from app.risk.options_risk import OptionsRiskProposal, evaluate_options_risk
from app.trading.order_manager import OrderRecord
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


# Same ceiling as `POST /orders`: every price and quantity column is
# `Numeric(18, 6)`, which holds at most 999999999999.999999.
_MAX_PREMIUM = 1e12


class ExecuteOptionLegRequest(BaseModel):
    symbol: str
    direction: Direction
    # Bounded for the same reason as `POST /orders`'s prices: untrusted
    # client input that is executed. A negative premium used to be
    # *approved and executed* -- `evaluate_options_risk` scored the
    # combination on a payoff built from it (a -100 premium reported
    # max_profit 17500), the batch went to the broker, and only then did
    # `MockBroker._resolve_fill_price` refuse the negative price. That
    # left one leg filled and one rejected: a partly executed batch, which
    # trips `_remediate_partial_batch` and halts the account. Malformed
    # input cost the caller a halt and an unwind.
    #
    # Zero and negative quantities, and non-finite values, were rejected
    # too -- but incidentally, by an exposure limit computed from the
    # *other* leg, or because every comparison against `inf`/NaN is False.
    # Neither is validation; both would stop protecting if a limit moved.
    quantity: float = Field(gt=0, lt=_MAX_PREMIUM)  # number of lots
    # Current market price per unit for this leg — no live options feed
    # exists in this environment (see docs/ARCHITECTURE.md), so this
    # mirrors POST /orders's `entry` field: MockBroker is fed this price
    # directly; a real broker ignores it and prices its own fill.
    premium: float = Field(gt=0, lt=_MAX_PREMIUM)


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
    # Does the combination the risk engine approved actually exist at the
    # broker now? False whenever any leg failed to establish -- see
    # `_remediate_partial_batch`. A client that ignores this and reads
    # only `max_loss` is reading the risk of a strategy that isn't there.
    strategy_intact: bool = True
    # What was done about it, in the order it was done. Empty when the
    # strategy is intact.
    remediation: list[str] = []


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


# A leg counts as established only when it is completely filled. Anything
# else -- rejected, unanswered, partially filled, acknowledged but never
# filled -- means the combination the risk engine approved does not exist
# at the broker, and the numbers in this response describe a strategy
# nobody is actually holding.
_ESTABLISHED_LEG_STATUSES = (OrderStatus.FILLED.value, OrderStatus.MONITORING.value)


async def _submit_leg(
    db: AsyncSession,
    stack,
    user: User,
    *,
    instrument: Instrument,
    symbol: str,
    direction: Direction,
    premium: float,
    total_quantity: float,
    idempotency_key: str,
    batch_id: uuid.UUID,
    strategy_name: str,
    execution_mode: ExecutionMode,
    transition_reason: str,
    audit_action: str,
) -> tuple[OrderRecord, float]:
    """Submits one leg as a real order and mirrors everything it did into
    Postgres (order row, position row, trade journal, notifications,
    audit), exactly as `POST /orders` does for a single trade.

    Returns that leg's final order and the quantity of *brand-new*
    exposure it opened -- 0.0 when it only reduced an existing position,
    was rejected, or never answered. That number is what
    `_remediate_partial_batch` has to give back when the rest of the
    combination fails to establish, so it is measured from the position's
    own before/after quantities rather than from the fill size: a fill
    that closes 30 and opens 20 the other way opened 20, not 50, and
    unwinding 50 would open a fresh position of its own.
    """
    if isinstance(stack.broker, MockBroker):
        stack.broker.set_quote(symbol, ltp=premium)

    existing_position = stack.position_manager.get(str(user.id), symbol)
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

    order, created = stack.order_manager.create_order(
        idempotency_key, str(user.id), symbol, direction, OrderType.MARKET, total_quantity
    )
    if created:
        stack.order_manager.transition(order.id, OrderStatus.VALIDATING)
        stack.order_manager.transition(order.id, OrderStatus.RISK_APPROVED, transition_reason)
        await stack.execution_engine.submit(order.id)
        stack.trades_today += 1

    final_order = stack.order_manager.get(order.id)
    if created:
        # Same "Repeated order rejection" tracking as app/api/orders.py's
        # place_order -- each leg is its own real order submitted to the
        # broker, so a run of consecutive broker-level rejections across
        # legs/strategies must trip `no_repeated_rejections` here too,
        # not just on the single-order path.
        stack.repeated_rejections = stack.repeated_rejections + 1 if final_order.status == OrderStatus.REJECTED else 0
    await persist_order(
        db, final_order, user.id, instrument.id, execution_mode=execution_mode, broker_account_id=stack.broker_account_id
    )

    opened_quantity = 0.0
    position_after = stack.position_manager.get(str(user.id), symbol)
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
            just_filled and position_after.is_open and position_after.is_long == (direction == Direction.LONG)
        )
        if just_filled:
            before_quantity = position_before["quantity"] if position_before is not None else 0.0
            after_quantity = position_after.quantity
            if after_quantity == 0:
                opened_quantity = 0.0
            elif before_quantity == 0 or (before_quantity > 0) != (after_quantity > 0):
                # Opened from flat, or flipped -- everything held now is new.
                opened_quantity = abs(after_quantity)
            else:
                opened_quantity = max(0.0, abs(after_quantity) - abs(before_quantity))
        position_row = await persist_position(
            db, user.id, instrument.id, position_after, execution_mode=execution_mode, source_key="manual"
        )
        realized_delta = position_after.realized_pnl - realized_pnl_before
        if realized_delta != 0 and position_before is not None:
            # Same fix as app/api/orders.py's place_order -- without
            # this, stack.daily_pnl/weekly_pnl never reflect a loss
            # realized by closing an options leg, so the
            # daily_loss_limit/weekly_loss_limit checks (both wired
            # into evaluate_options_risk above) could never fail no
            # matter how much this specific path lost.
            stack.daily_pnl += realized_delta
            stack.weekly_pnl += realized_delta
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
                # Same fix as app/api/orders.py's record_trade call --
                # the quantity this fill actually closed, not the whole
                # pre-fill position, which on a partial reduce left the
                # journal row disagreeing with its own `pnl`.
                quantity=min(final_order.filled_quantity, abs(position_before["quantity"])),
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
                title=f"{symbol} position closed",
                body=f"Realized P&L: {realized_delta:.2f}",
                data={"symbol": symbol, "pnl": realized_delta},
            )
        if opened_or_added:
            await create_notification(
                db,
                user_id=user.id,
                notification_type=NotificationType.TRADE_EXECUTED,
                title=f"{symbol} order executed",
                body=f"Opened {direction.value} position in {symbol}",
                data={"symbol": symbol, "direction": direction.value, "strategy_name": strategy_name},
            )

    await record_audit(
        db,
        actor="user",
        action=audit_action,
        user_id=user.id,
        details={
            "batch_id": str(batch_id),
            "strategy_name": strategy_name,
            "symbol": symbol,
            "direction": direction.value,
            "status": final_order.status.value,
        },
    )
    return final_order, opened_quantity


async def _remediate_partial_batch(
    db: AsyncSession,
    stack,
    user: User,
    *,
    batch_id: uuid.UUID,
    strategy_name: str,
    execution_mode: ExecutionMode,
    leg_results: list[LegExecutionResult],
    opened: list[tuple[str, Instrument, Direction, float, float]],
) -> tuple[bool, list[str]]:
    """Deals with a multi-leg strategy that only partly executed.

    Blueprint §37-40 gates a combination on its *combined* payoff: a bull
    put spread's approved max loss is the width of the spread because the
    long put caps the short one. Nothing at the exchange enforces that
    pairing -- each leg is a separate order, and neither Upstox nor Dhan
    offers an atomic multi-leg submission this codebase can use. So when
    the short leg fills and the protective long leg is rejected, the
    account is left holding a naked short option whose real risk is the
    strike, not the spread width: measured on a 25200/25000 put spread,
    an approved max loss of 6,500 became an actual 1,254,000 on a 100,000
    account, reported as `201 Created` with no halt and a "position
    opened" notification. The risk engine's approval was conditional on a
    combination that no longer exists.

    Two rules, and the difference between them matters:

    * A leg whose fate is **unknown** (`FAILED` -- the broker never
      answered; see `UpstoxBroker.place_order`) is never unwound. An
      opposing order for a leg that may or may not exist at the exchange
      is not a reversal, it is a coin flip that can open a fresh position
      of its own. Those go to reconciliation, not to remediation.
    * Otherwise every leg's fate is known, so the exposure this call
      newly opened is given back with opposing market orders -- the only
      state the risk engine actually approved is the one before the call.

    Either way the account is halted (blueprint §73-75) whenever anything
    executed, because an unwind is best effort: it can be rejected in
    turn, it does not restore a leg this batch merely *reduced*, and the
    condition that rejected a leg (margin, a freeze quantity, a
    non-tradable contract) is likely to reject the next order too. The
    halt exempts reducing orders, so the holder can still get out; what
    it stops is piling more on top of a position nobody approved.

    Returns `(strategy_intact, remediation_lines)`.
    """
    established = [r for r in leg_results if r.status in _ESTABLISHED_LEG_STATUSES]
    unknown = [r for r in leg_results if r.status == OrderStatus.FAILED.value]
    if not unknown and len(established) == len(leg_results):
        return True, []
    if not established and not unknown:
        # Every leg was rejected outright: nothing reached the exchange,
        # so there is no imbalance to remediate and nothing to halt over.
        return False, ["No leg reached the exchange -- nothing was opened, so nothing needed unwinding."]

    remediation: list[str] = []
    if unknown:
        remediation.append(
            f"{len(unknown)} leg(s) went unanswered by the broker; their fate is unknown. Filled legs were "
            "deliberately NOT unwound -- an opposing order for a leg that may already exist at the exchange "
            "would open a position of its own. Reconcile against the broker."
        )
    else:
        for symbol, instrument, direction, quantity, premium in opened:
            position = stack.position_manager.get(str(user.id), symbol)
            closable = min(quantity, abs(position.quantity)) if position is not None else 0.0
            if closable <= 0:
                remediation.append(f"{symbol}: nothing left to unwind.")
                continue
            exit_direction = Direction.SHORT if direction == Direction.LONG else Direction.LONG
            unwind_order, _ = await _submit_leg(
                db,
                stack,
                user,
                instrument=instrument,
                symbol=symbol,
                direction=exit_direction,
                premium=premium,
                total_quantity=closable,
                idempotency_key=f"unwind:{user.id}:{batch_id}:{symbol}",
                batch_id=batch_id,
                strategy_name=strategy_name,
                execution_mode=execution_mode,
                transition_reason=f"unwinding partially executed options batch {batch_id}",
                audit_action="options_strategy.leg_unwound",
            )
            if unwind_order.status.value in _ESTABLISHED_LEG_STATUSES:
                remediation.append(f"{symbol}: unwound {closable} of newly opened exposure.")
            else:
                remediation.append(
                    f"{symbol}: unwind order came back {unwind_order.status.value} "
                    f"({unwind_order.rejection_reason or 'no reason given'}) -- exposure is still open."
                )
        if not opened:
            remediation.append(
                "No leg opened new exposure, so there was nothing to unwind -- the incomplete legs left an "
                "existing position only partly closed."
            )

    reason = f"Multi-leg options strategy '{strategy_name}' (batch {batch_id}) only partly executed"
    await halt_account(str(user.id), reason)
    await create_notification(
        db,
        user_id=user.id,
        notification_type=NotificationType.RECONCILIATION_REQUIRED,
        title=f"{strategy_name} did not execute as approved",
        body="; ".join(remediation)[:1000],
        data={
            "batch_id": str(batch_id),
            "strategy_name": strategy_name,
            "legs": [{"symbol": r.symbol, "status": r.status} for r in leg_results],
        },
    )
    await record_audit(
        db,
        actor="system",
        action="options_strategy.partial_execution",
        user_id=user.id,
        details={"batch_id": str(batch_id), "strategy_name": strategy_name, "remediation": remediation},
    )
    remediation.append(f"New entries are halted for this account: {reason}")
    return False, remediation


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
    exchange-level atomic multi-leg fills. This is not atomic and does
    not pretend to be; what it does guarantee is that a batch which only
    partly executes never quietly leaves the account holding something
    the risk engine did not approve. `_remediate_partial_batch` unwinds
    the exposure this call newly opened (never a leg whose fate is
    unknown) and halts the account. `strategy_intact` in the response
    says whether the approved combination actually exists at the broker;
    `remediation` says what was done when it doesn't.
    """
    if not payload.legs:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "At least one leg is required")

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

    # Does every leg oppose an open position in its own instrument? Only
    # then can this order do nothing but reduce what the account already
    # holds, and only then may it skip the entry-only limits below.
    # All-or-nothing on purpose: one leg that opens exposure makes the
    # whole order an entry, so a fresh position can never ride in
    # alongside a genuine close. Each reducing leg is additionally clamped
    # to the open quantity where it is submitted, so an exempted order
    # cannot open or flip a leg either.
    #
    # `POST /options/execute` is the only path that closes an options
    # position, so an exit refused here is a position the holder cannot
    # get out of -- the same trap `POST /orders` had.
    leg_positions = {leg.symbol: stack.position_manager.get(str(user.id), leg.symbol) for leg in payload.legs}
    is_reducing = bool(payload.legs) and all(
        (position := leg_positions[leg.symbol]) is not None
        and position.is_open
        and position.is_long != (leg.direction == Direction.LONG)
        for leg in payload.legs
    )

    halt_reason = await account_halt_reason(str(user.id))
    if halt_reason is not None and not is_reducing:
        # Gated on `not is_reducing` to match this message's own wording,
        # and because reconciliation halts an account precisely when its
        # positions look wrong -- the worst moment to forbid closing them.
        raise HTTPException(status.HTTP_423_LOCKED, f"New entries are halted for this account: {halt_reason}")
    # Blueprint §56/§57: rolls stack.trades_today/daily_pnl/weekly_pnl at a
    # day/week boundary -- see _UserTradingStack._roll_risk_window. Without
    # this, an options strategy submitted right after midnight would still
    # be evaluated against yesterday's counters, the same bug this call
    # already prevents for POST /orders.
    stack._roll_risk_window(datetime.now(timezone.utc))
    open_positions = stack.position_manager.open_positions(str(user.id))
    current_exposure = sum(abs(p.quantity) * p.average_price for p in open_positions)

    risk_proposal = OptionsRiskProposal(
        account_id=str(user.id),
        is_reducing=is_reducing,
        account_balance=(await stack.broker.get_account()).balance,
        current_exposure=current_exposure,
        payoff=payoff,
        broker_healthy=await stack.broker.is_healthy(),
        open_positions=len(open_positions),
        trades_today=stack.trades_today,
        daily_pnl=stack.daily_pnl,
        weekly_pnl=stack.weekly_pnl,
        repeated_rejections=stack.repeated_rejections,
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
    # Everything this call newly opened, in submission order:
    # (symbol, instrument, direction, quantity opened, premium). This is
    # what has to be given back if a later leg fails to establish -- see
    # `_remediate_partial_batch`.
    opened: list[tuple[str, Instrument, Direction, float, float]] = []
    for leg in payload.legs:
        instrument = instruments[leg.symbol]
        existing_position = stack.position_manager.get(str(user.id), leg.symbol)
        total_quantity = leg.quantity * instrument.lot_size
        if is_reducing and existing_position is not None:
            # What makes the exemption above safe rather than a hole: an
            # order that skipped the entry limits can only ever reduce or
            # flatten each leg, never open or flip one. Without this, a
            # client could clear every limit by sending an oversized
            # opposing leg and calling it a close.
            total_quantity = min(total_quantity, abs(existing_position.quantity))

        final_order, opened_quantity = await _submit_leg(
            db,
            stack,
            user,
            instrument=instrument,
            symbol=leg.symbol,
            direction=leg.direction,
            premium=leg.premium,
            total_quantity=total_quantity,
            idempotency_key=f"{user.id}:{batch_id}:{leg.symbol}",
            batch_id=batch_id,
            strategy_name=payload.strategy_name,
            execution_mode=execution_mode,
            transition_reason=f"options strategy batch {batch_id}",
            audit_action="options_strategy.leg_placed",
        )
        if opened_quantity > 0:
            opened.append((leg.symbol, instrument, leg.direction, opened_quantity, leg.premium))
        leg_results.append(
            LegExecutionResult(
                symbol=leg.symbol, order_id=final_order.id, status=final_order.status.value, rejection_reason=final_order.rejection_reason
            )
        )

    # Each leg is a separate order and nothing at the exchange enforces
    # the combination -- so before answering, check that the combination
    # the risk engine approved actually exists, and deal with it if not.
    strategy_intact, remediation = await _remediate_partial_batch(
        db,
        stack,
        user,
        batch_id=batch_id,
        strategy_name=payload.strategy_name,
        execution_mode=execution_mode,
        leg_results=leg_results,
        opened=opened,
    )

    return ExecuteOptionsStrategyResponse(
        batch_id=batch_id,
        max_profit=payoff.max_profit,
        max_loss=payoff.max_loss,
        net_premium=payoff.net_premium,
        capital_requirement=payoff.capital_requirement,
        liquidity_warnings=liquidity_warnings,
        legs=leg_results,
        strategy_intact=strategy_intact,
        remediation=remediation,
    )
