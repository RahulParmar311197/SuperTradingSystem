import asyncio
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_permission
from app.brokers.base import BrokerError
from app.brokers.mock import MockBroker
from app.core.audit import record_audit
from app.core.metrics import ORDER_COUNT, RISK_REJECTION_COUNT
from app.core.redis import account_halt_reason, channel_name, get_latest_price, get_price_age_seconds, get_price_jump_pct, publish
from app.database.models.instruments import Instrument
from app.database.models.notifications import NotificationType
from app.database.models.risk import RiskDecision as RiskEventDecision
from app.database.models.risk import RiskEvent
from app.database.models.strategy import Direction
from app.database.models.trading import ExecutionMode, OrderStatus, OrderType
from app.database.models.users import TradingPermission, User
from app.database.session import get_db
from app.notifications.service import create_notification
from app.risk.engine import RiskEngine, TradeRiskProposal, calculate_position_size
from app.risk.kill_switch import load_kill_switch_state
from app.risk.limits import RiskLimits
from app.risk.portfolio import compute_correlated_exposure
from app.trading.broker_resolver import resolve_broker
from app.trading.execution import ExecutionEngine
from app.trading.order_manager import OrderManager
from app.trading.persistence import persist_order, persist_position, record_trade
from app.trading.position_manager import PositionManager, PositionRecord

router = APIRouter(tags=["trading"])


async def _get_instrument_by_symbol(db: AsyncSession, symbol: str) -> Instrument:
    """Orders are persisted against a real `instruments` row (blueprint
    §9-13), so the symbol must already be registered via
    `POST /instruments` — this never silently creates one."""
    instrument = (
        await db.execute(select(Instrument).where(Instrument.symbol == symbol, Instrument.active.is_(True)))
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown instrument symbol: {symbol}")
    return instrument


class _UserTradingStack:
    """Per-user order/position/risk/broker stack.

    The order state machine and position math (`OrderManager`,
    `PositionManager`) live in memory for the lifetime of the API process
    — see `app.trading.persistence` for how their results get mirrored
    into Postgres (`orders`/`order_events`/`positions`/`trades`) after
    every transition, so a restart, the reconciliation worker, and the
    trade journal (blueprint §61) all see real data. `broker` comes from
    `app.trading.broker_resolver.resolve_broker` — `MockBroker` for a user
    with no connected account (Stage 9's honest default), a real
    `UpstoxBroker`/`DhanBroker` for one who has connected one.
    """

    def __init__(self, broker, broker_account_id: uuid.UUID | None = None) -> None:
        self.broker = broker
        self.broker_account_id = broker_account_id
        self.order_manager = OrderManager()
        self.position_manager = PositionManager()
        self.risk_engine = RiskEngine(limits=RiskLimits())
        self.execution_engine = ExecutionEngine(self.broker, self.order_manager, self.position_manager)
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0
        # Blueprint §57 "Repeated order rejection" -- consecutive
        # broker-level rejections (see `place_order`'s update after each
        # fresh order attempt below).
        self.repeated_rejections = 0
        # Set on first `_roll_risk_window` call, not here -- `None` means
        # "no window established yet", so the first call never wrongly
        # resets a stack that was just created.
        self._risk_day = None
        self._risk_week = None

    def _roll_risk_window(self, now: datetime) -> None:
        """Resets `trades_today`/`daily_pnl` at a UTC day boundary and
        `weekly_pnl` at a UTC (ISO) week boundary. Without this, these
        counters only ever reset when the API process restarts, making
        RiskEngine.evaluate's `max_trades_per_day`/`daily_loss_limit`/
        `weekly_loss_limit` checks lifetime-of-process limits rather than
        the rolling daily/weekly limits they're meant to be -- see
        docs/ARCHITECTURE.md. Mirrors
        `PaperTradingEngine._roll_risk_window` (app/paper/engine.py),
        which does the same thing keyed off candle time instead of wall
        clock."""
        today = now.date()
        this_week = now.isocalendar()[:2]
        if self._risk_day is not None and today != self._risk_day:
            self.trades_today = 0
            self.daily_pnl = 0.0
        if self._risk_week is not None and this_week != self._risk_week:
            self.weekly_pnl = 0.0
        self._risk_day = today
        self._risk_week = this_week


_STACKS: dict[uuid.UUID, _UserTradingStack] = {}
_STACK_LOCKS: dict[uuid.UUID, asyncio.Lock] = {}


async def _stack_for(user: User, db: AsyncSession) -> _UserTradingStack:
    """Resolves (and caches) the trading stack for `user`. The broker is
    resolved once, at first use — connecting or disconnecting a broker
    account takes effect on this stack's next process restart, the same
    documented limitation as the rest of this in-memory stack (see
    docs/ARCHITECTURE.md's "Multiple API replicas for the manual /orders
    path").

    `resolve_broker` awaits a real DB query, so two concurrent first
    requests for the same user (e.g. two browser tabs opening right after
    login) could both see `user.id not in _STACKS`, each build their own
    `_UserTradingStack`, and have the second silently clobber the first in
    the dict — losing whatever the first stack's `OrderManager` already
    knew about (an order it had just placed becomes invisible to every
    later `GET /orders`/`GET /positions`, since those read the in-memory
    manager, not Postgres). A per-user lock serializes stack creation;
    `dict.setdefault` for the lock itself is safe since plain dict access
    between two `await` points can't interleave on one event loop."""
    if user.id not in _STACKS:
        lock = _STACK_LOCKS.setdefault(user.id, asyncio.Lock())
        async with lock:
            if user.id not in _STACKS:
                broker, broker_account_id = await resolve_broker(db, user)
                _STACKS[user.id] = _UserTradingStack(broker, broker_account_id)
    return _STACKS[user.id]


def all_stacks() -> dict[uuid.UUID, _UserTradingStack]:
    """A snapshot of every trading stack this API process currently holds
    — used by `app.trading.live_reconciliation` to reconcile whichever
    connected accounts have actually placed an order this process
    lifetime. Not for mutation; callers get a shallow copy."""
    return dict(_STACKS)


async def _mark_open_positions_to_market(stack: "_UserTradingStack", user_id: str) -> list[PositionRecord]:
    """Blueprint §60: `PositionManager` tracks `unrealized_pnl`, and
    `mark_to_market` computes it correctly -- but on this (live/manual)
    path nothing ever called it. `apply_fill` (on every order fill) only
    ever sets `quantity`/`average_price`/`realized_pnl`; it has no reason
    to know the current market price, and nothing else in this path calls
    `mark_to_market` either, unlike `PaperTradingEngine.on_candle`, which
    does so every candle. Without this, every live/manual position's
    `unrealized_pnl` -- read here by `GET /positions` and `GET
    /portfolio`, and persisted verbatim into the `positions` table by
    `app.trading.persistence.persist_position` -- stayed frozen at its
    dataclass default of 0.0 forever, no matter how far price moved,
    silently misreporting real P&L on the one path that risks real money.
    Uses whatever price `MarketDataWorker` last cached in Redis; a symbol
    with no cached tick yet is left as-is rather than guessed at."""
    positions = stack.position_manager.open_positions(user_id)
    for position in positions:
        price = await get_latest_price(position.symbol)
        if price is not None:
            stack.position_manager.mark_to_market(user_id, position.symbol, price)
    return positions


def _execution_mode_for(stack: "_UserTradingStack") -> ExecutionMode:
    """Blueprint §101: "Never make paper and live look identical" — a
    stack with no connected broker account trades against `MockBroker`
    (Stage 9's honest default), so anything it persists is PAPER, not
    LIVE, regardless of which trading permission gated the request."""
    return ExecutionMode.PAPER if isinstance(stack.broker, MockBroker) else ExecutionMode.LIVE


class PlaceOrderRequest(BaseModel):
    symbol: str
    direction: Direction
    order_type: OrderType = OrderType.MARKET
    entry: float
    stop: float
    price: float | None = None


class OrderResponse(BaseModel):
    id: uuid.UUID
    symbol: str
    direction: str
    status: str
    quantity: float
    broker_order_id: str | None
    rejection_reason: str | None


async def _publish_order_event(user: User, order) -> None:
    try:
        await publish(channel_name("orders", str(user.id)), _to_response(order).model_dump())
    except Exception:
        pass  # best-effort fanout; never fail the request because a websocket relay is down


async def _publish_position_snapshot(user: User, stack: "_UserTradingStack") -> None:
    try:
        positions = stack.position_manager.open_positions(str(user.id))
        await publish(
            channel_name("positions", str(user.id)),
            {"positions": [{"symbol": p.symbol, "quantity": p.quantity, "average_price": p.average_price} for p in positions]},
        )
    except Exception:
        pass


def _to_response(order) -> OrderResponse:
    return OrderResponse(
        id=order.id,
        symbol=order.symbol,
        direction=order.direction.value,
        status=order.status.value,
        quantity=order.quantity,
        broker_order_id=order.broker_order_id,
        rejection_reason=order.rejection_reason,
    )


@router.post("/orders", response_model=OrderResponse, status_code=status.HTTP_201_CREATED)
async def place_order(
    payload: PlaceOrderRequest,
    user: User = Depends(require_permission(TradingPermission.LIVE_TRADE)),
    db: AsyncSession = Depends(get_db),
) -> OrderResponse:
    halt_reason = await account_halt_reason(str(user.id))
    if halt_reason is not None:
        raise HTTPException(status.HTTP_423_LOCKED, f"New entries are halted for this account: {halt_reason}")

    instrument = await _get_instrument_by_symbol(db, payload.symbol)

    stack = await _stack_for(user, db)
    stack._roll_risk_window(datetime.now(timezone.utc))
    if isinstance(stack.broker, MockBroker):
        # Only MockBroker needs a quote fed in — a real broker gets its
        # own price from the market, not from what the client submitted
        # as "entry". Never let a real order's fill price be dictated by
        # the caller.
        stack.broker.set_quote(payload.symbol, ltp=payload.entry)

    # Blueprint §56/§57: `payload.entry` is otherwise trusted input used
    # both to size the position (calculate_position_size) and to size that
    # same position's notional risk checks (exposure_limit et al.) --
    # unchecked, a client could pick `entry` close to `stop` to inflate
    # quantity while those notional checks, computed from the same forged
    # entry, still look small. Comparing against the broker's own quote
    # closes that gap; for MockBroker this is always 0% since the seed
    # above just set its quote to `payload.entry`.
    quote = await stack.broker.get_quote(payload.symbol)
    entry_deviation_pct = abs(payload.entry - quote.ltp) / quote.ltp * 100 if quote.ltp else 0.0

    account = await stack.broker.get_account()
    open_positions = stack.position_manager.open_positions(str(user.id))
    current_exposure = sum(abs(p.quantity) * p.average_price for p in open_positions)
    other_position_notionals = {
        p.symbol: abs(p.quantity) * p.average_price for p in open_positions if p.symbol != payload.symbol
    }
    correlated_exposure = await compute_correlated_exposure(
        db,
        target_symbol=payload.symbol,
        target_notional=0.0,  # the engine adds this trade's own sized notional itself
        open_position_notionals=other_position_notionals,
        threshold=stack.risk_engine.limits.correlation_threshold,
    )
    proposal = TradeRiskProposal(
        account_id=str(user.id),
        strategy_id=None,
        entry=payload.entry,
        stop=payload.stop,
        account_balance=account.balance,
        open_positions=len(open_positions),
        trades_today=stack.trades_today,
        daily_pnl=stack.daily_pnl,
        weekly_pnl=stack.weekly_pnl,
        current_exposure=current_exposure,
        strategy_allocation=0.0,
        # No live feed is wired for this symbol yet if this comes back None
        # (see app.workers.market_data_worker) — nothing to be stale
        # relative to, so treat that case as fresh rather than blocking
        # every order in a system with no broker connected.
        market_data_age_seconds=await get_price_age_seconds(payload.symbol) or 0.0,
        broker_healthy=await stack.broker.is_healthy(),
        correlated_exposure=correlated_exposure,
        repeated_rejections=stack.repeated_rejections,
        # Same "nothing to compare against yet" convention as
        # market_data_age_seconds above -- no prior tick means no jump to
        # flag, not an infinite one.
        recent_price_jump_pct=await get_price_jump_pct(payload.symbol) or 0.0,
        entry_deviation_pct=entry_deviation_pct,
    )
    # Blueprint §58: refreshed on every call, not cached on the stack at
    # construction time, so a kill triggered via the admin endpoint from
    # this or any other process takes effect on the very next order --
    # see app.risk.kill_switch.load_kill_switch_state.
    stack.risk_engine.kill_switch = await load_kill_switch_state(str(user.id), None)
    decision = stack.risk_engine.evaluate(proposal)
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
        RISK_REJECTION_COUNT.inc()
        # Blueprint §63 mandates "Order rejected"/"Daily loss limit"
        # notifications -- the paper (app/api/paper.py) and autonomous
        # (app/workers/auto_trade_worker.py) trading paths both already
        # notify on a risk rejection; this, the one path that handles real
        # broker money, used to only raise this HTTPException and write
        # the RiskEvent row above -- nothing else in the system (another
        # device, GET /notifications, an admin view) ever learned a live
        # order was blocked.
        failed_checks = decision.failed_checks
        notification_type = (
            NotificationType.DAILY_LOSS_LIMIT
            if failed_checks and failed_checks[0].name == "daily_loss_limit"
            else NotificationType.ORDER_REJECTED
        )
        await create_notification(
            db,
            user_id=user.id,
            notification_type=notification_type,
            title=f"{payload.symbol} order rejected",
            body=decision.reason or "Risk engine rejected this trade",
            data={"symbol": payload.symbol, "reason": decision.reason},
        )
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Risk engine rejected this trade: {decision.reason}")

    quantity = calculate_position_size(
        account.balance, stack.risk_engine.limits.risk_per_trade_pct, payload.entry, payload.stop, stack.risk_engine.limits.max_position_size
    )
    idempotency_key = f"{user.id}:{payload.symbol}:{payload.direction.value}:{payload.entry}:{payload.stop}"
    order, created = stack.order_manager.create_order(
        idempotency_key, str(user.id), payload.symbol, payload.direction, payload.order_type, quantity, payload.price
    )

    # Snapshot the position *values* (not the PositionRecord reference —
    # apply_fill mutates it in place, so holding the object itself would
    # alias the post-fill state) before the fill, so a closing/reducing
    # fill can be told apart from one that only opens or adds — that's
    # what decides whether a `trades` journal row (blueprint §61) gets
    # written below.
    existing_position = stack.position_manager.get(str(user.id), payload.symbol)
    position_before = (
        {
            "is_long": existing_position.is_long,
            "quantity": existing_position.quantity,
            "average_price": existing_position.average_price,
            "stop": existing_position.stop,
            "target": existing_position.target,
            "realized_pnl": existing_position.realized_pnl,
        }
        if existing_position is not None
        else None
    )
    realized_pnl_before = position_before["realized_pnl"] if position_before else 0.0

    if created:
        stack.order_manager.transition(order.id, OrderStatus.VALIDATING)
        stack.order_manager.transition(order.id, OrderStatus.RISK_APPROVED)
        await stack.execution_engine.submit(order.id)
        stack.trades_today += 1

    final_order = stack.order_manager.get(order.id)
    if created:
        # Blueprint §57 "Repeated order rejection" -- a run of consecutive
        # broker-level rejections (insufficient margin, bad symbol, etc.,
        # distinct from a risk-engine pre-trade REJECT, which never
        # reaches the broker at all) trips `no_repeated_rejections` above
        # on the next attempt. Reset on any order that actually executes,
        # so a resolved issue doesn't permanently trip this.
        stack.repeated_rejections = stack.repeated_rejections + 1 if final_order.status == OrderStatus.REJECTED else 0
    ORDER_COUNT.labels(final_order.status.value).inc()

    execution_mode = _execution_mode_for(stack)
    await persist_order(
        db, final_order, user.id, instrument.id, execution_mode=execution_mode, broker_account_id=stack.broker_account_id
    )

    position_after = stack.position_manager.get(str(user.id), payload.symbol)
    if position_after is not None:
        # A genuine fill actually happened on this call -- as opposed to
        # `position_after` merely reflecting an existing position
        # unchanged by a broker-rejected order (ExecutionEngine.submit
        # never calls apply_fill when the broker rejects, see below).
        just_filled = created and final_order.status in (
            OrderStatus.FILLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.MONITORING,
        )
        opened_or_added = (
            just_filled and position_after.is_open and position_after.is_long == (payload.direction == Direction.LONG)
        )
        if position_after.is_open and position_after.is_long == (payload.direction == Direction.LONG):
            # Blueprint §60: `payload.stop` is required input already used
            # to size this order (calculate_position_size) and by the risk
            # engine, but nothing ever attached it to the resulting
            # position -- so no live position ever had a recorded stop,
            # unlike PaperTradingEngine._maybe_enter (app/paper/engine.py),
            # which already does this for every paper/auto-trade entry.
            # Only set it when this fill opened, added to, or flipped into
            # a position in payload.direction -- never on a fill that only
            # reduces/closes a position in the other direction, whose stop
            # belongs to that original entry, not this one.
            position_after.stop = payload.stop
        position_row = await persist_position(db, user.id, instrument.id, position_after, execution_mode=execution_mode)
        realized_delta = position_after.realized_pnl - realized_pnl_before
        if realized_delta != 0 and position_before is not None:
            # Mirrors PaperTradingEngine._maybe_exit (app/paper/engine.py) --
            # without this, stack.daily_pnl/weekly_pnl stay 0.0 forever, and
            # RiskEngine.evaluate's daily_loss_limit/weekly_loss_limit checks
            # (app/risk/engine.py) can never fail for this path no matter how
            # much the account has actually lost.
            stack.daily_pnl += realized_delta
            stack.weekly_pnl += realized_delta
            await record_trade(
                db,
                user_id=user.id,
                instrument_id=instrument.id,
                direction=Direction.LONG if position_before["is_long"] else Direction.SHORT,
                entry_price=position_before["average_price"],
                # The real broker fill price, not the client-supplied
                # `payload.entry` -- `pnl` above is computed from this same
                # value (via PositionManager.apply_fill -> ExecutionEngine.
                # submit's `result.average_fill_price`), so persisting
                # `payload.entry` here made the trades journal row
                # internally inconsistent (exit_price didn't match the pnl/
                # entry_price actually realized) for any real broker, where
                # the fill price is independent of what the client typed as
                # `entry`. Invisible with MockBroker because
                # `stack.broker.set_quote(payload.symbol, ltp=payload.entry)`
                # above pins the simulated fill to that same value.
                exit_price=final_order.average_fill_price,
                # The quantity this fill actually closed, not the whole
                # pre-fill position. This block runs on any `realized_delta
                # != 0`, which includes a *partial* reduce -- closing 40 of
                # a 100-unit position recorded `quantity=100` alongside a
                # `pnl` covering only those 40 units, so the row did not
                # agree with itself (100 units moving 100 -> 120 is 2000,
                # not the 800 recorded). It also double-counted: the later
                # close of the remaining 60 journals another row, summing to
                # 160 units for a 100-unit position. `min` is what makes
                # this right for a flip too -- selling 120 against a 100-unit
                # long closes 100 and opens a fresh 20 short, and the
                # realized P&L covers only the 100 that closed.
                quantity=min(final_order.filled_quantity, abs(position_before["quantity"])),
                pnl=realized_delta,
                stop=position_before["stop"],
                target=position_before["target"],
                position_id=position_row.id,
                execution_mode=execution_mode,
            )
            # Blueprint §63/§104 list "Position closed" as a required
            # notification event -- POST /orders is the one path that
            # handles real broker money and used to notify only on
            # rejection (see the create_notification call above), never
            # on an actual fill. There's no live stop-loss/take-profit
            # enforcement worker yet (see docs/ARCHITECTURE.md), so unlike
            # PaperTradingEngine there's no exit_reason to distinguish
            # SL_HIT/TP_HIT from an ordinary manual close -- every live
            # close is POSITION_CLOSED.
            await create_notification(
                db,
                user_id=user.id,
                notification_type=NotificationType.POSITION_CLOSED,
                title=f"{payload.symbol} position closed",
                body=f"Realized P&L: {realized_delta:.2f}",
                data={"symbol": payload.symbol, "pnl": realized_delta},
            )
        if opened_or_added:
            # Same blueprint requirement, the open side -- fired once per
            # order that actually filled and opened/added to/flipped into
            # a position in payload.direction (never on a broker-rejected
            # order, which leaves `position_after` reflecting whatever
            # existed before this call, unchanged).
            await create_notification(
                db,
                user_id=user.id,
                notification_type=NotificationType.TRADE_EXECUTED,
                title=f"{payload.symbol} order executed",
                body=f"Opened {payload.direction.value} position in {payload.symbol}",
                data={"symbol": payload.symbol, "direction": payload.direction.value},
            )

    await record_audit(
        db,
        actor="user",
        action="order.placed",
        user_id=user.id,
        details={"symbol": payload.symbol, "direction": payload.direction.value, "status": final_order.status.value},
    )
    await _publish_order_event(user, final_order)
    await _publish_position_snapshot(user, stack)
    return _to_response(final_order)


@router.get("/orders", response_model=list[OrderResponse])
async def list_orders(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[OrderResponse]:
    stack = await _stack_for(user, db)
    return [_to_response(o) for o in stack.order_manager.list_all(str(user.id))]


@router.post("/orders/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: uuid.UUID,
    user: User = Depends(require_permission(TradingPermission.LIVE_TRADE)),
    db: AsyncSession = Depends(get_db),
) -> OrderResponse:
    stack = await _stack_for(user, db)
    order = stack.order_manager.get(order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status not in (OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot cancel an order in status {order.status.value}")

    if order.broker_order_id:
        # A broker-level cancel failure (e.g. the order already filled or
        # was already cancelled in the meantime -- an ordinary race, not a
        # bug) must never look like a successful cancel, and must never be
        # allowed to propagate as a raw exception either: that would 500
        # the request *and* leave the order's true broker-side status
        # unreconciled with its still-SUBMITTED/ACKNOWLEDGED local status,
        # since none of the transition/persist/audit lines below would run.
        # Surfacing it as a clean error and leaving local state untouched
        # is safer than guessing at a new status -- the existing
        # reconciliation worker is what should ultimately correct it if the
        # broker's real status has actually moved on.
        try:
            await stack.broker.cancel_order(order.broker_order_id)
        except BrokerError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, f"Broker failed to cancel this order: {exc}") from exc
    stack.order_manager.transition(order_id, OrderStatus.CANCELLED, "cancelled by user")
    final_order = stack.order_manager.get(order_id)
    ORDER_COUNT.labels(final_order.status.value).inc()

    instrument = await _get_instrument_by_symbol(db, final_order.symbol)
    await persist_order(
        db,
        final_order,
        user.id,
        instrument.id,
        execution_mode=_execution_mode_for(stack),
        broker_account_id=stack.broker_account_id,
    )

    await record_audit(db, actor="user", action="order.cancelled", user_id=user.id, details={"order_id": str(order_id)})
    await _publish_order_event(user, final_order)
    return _to_response(final_order)
