import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.brokers.mock import MockBroker
from app.core.audit import record_audit
from app.core.metrics import ORDER_COUNT, RISK_REJECTION_COUNT
from app.core.redis import account_halt_reason, channel_name, get_price_age_seconds, publish
from app.database.models.risk import RiskDecision as RiskEventDecision
from app.database.models.risk import RiskEvent
from app.database.models.strategy import Direction
from app.database.models.trading import OrderStatus, OrderType
from app.database.models.users import User
from app.database.session import get_db
from app.risk.engine import RiskEngine, TradeRiskProposal, calculate_position_size
from app.risk.limits import RiskLimits
from app.trading.execution import ExecutionEngine
from app.trading.order_manager import OrderManager
from app.trading.position_manager import PositionManager

router = APIRouter(tags=["trading"])


class _UserTradingStack:
    """Per-user in-memory order/position/risk/broker stack.

    A real deployment persists orders/positions in Postgres and resolves
    the broker from the user's connected `BrokerAccount` (Dhan/Upstox);
    until those adapters are wired in (see app/brokers/dhan, app/brokers/upstox),
    this uses `MockBroker` so the API surface and risk gate are exercisable
    end-to-end today.
    """

    def __init__(self) -> None:
        self.broker = MockBroker(starting_balance=100_000.0)
        self.order_manager = OrderManager()
        self.position_manager = PositionManager()
        self.risk_engine = RiskEngine(limits=RiskLimits())
        self.execution_engine = ExecutionEngine(self.broker, self.order_manager, self.position_manager)
        self.trades_today = 0
        self.daily_pnl = 0.0
        self.weekly_pnl = 0.0


_STACKS: dict[uuid.UUID, _UserTradingStack] = {}


def _stack_for(user: User) -> _UserTradingStack:
    if user.id not in _STACKS:
        _STACKS[user.id] = _UserTradingStack()
    return _STACKS[user.id]


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
    payload: PlaceOrderRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> OrderResponse:
    halt_reason = await account_halt_reason(str(user.id))
    if halt_reason is not None:
        raise HTTPException(status.HTTP_423_LOCKED, f"New entries are halted for this account: {halt_reason}")

    stack = _stack_for(user)
    stack.broker.set_quote(payload.symbol, ltp=payload.entry)

    account = await stack.broker.get_account()
    proposal = TradeRiskProposal(
        account_id=str(user.id),
        strategy_id=None,
        entry=payload.entry,
        stop=payload.stop,
        account_balance=account.balance,
        open_positions=len(stack.position_manager.open_positions(str(user.id))),
        trades_today=stack.trades_today,
        daily_pnl=stack.daily_pnl,
        weekly_pnl=stack.weekly_pnl,
        current_exposure=0.0,
        strategy_allocation=0.0,
        # No live feed is wired for this symbol yet if this comes back None
        # (see app.workers.market_data_worker) — nothing to be stale
        # relative to, so treat that case as fresh rather than blocking
        # every order in a system with no broker connected.
        market_data_age_seconds=await get_price_age_seconds(payload.symbol) or 0.0,
        broker_healthy=await stack.broker.is_healthy(),
    )
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"Risk engine rejected this trade: {decision.reason}")

    quantity = calculate_position_size(
        account.balance, stack.risk_engine.limits.risk_per_trade_pct, payload.entry, payload.stop, stack.risk_engine.limits.max_position_size
    )
    idempotency_key = f"{user.id}:{payload.symbol}:{payload.direction.value}:{payload.entry}:{payload.stop}"
    order, created = stack.order_manager.create_order(
        idempotency_key, str(user.id), payload.symbol, payload.direction, payload.order_type, quantity, payload.price
    )
    if created:
        stack.order_manager.transition(order.id, OrderStatus.VALIDATING)
        stack.order_manager.transition(order.id, OrderStatus.RISK_APPROVED)
        await stack.execution_engine.submit(order.id)
        stack.trades_today += 1

    final_order = stack.order_manager.get(order.id)
    ORDER_COUNT.labels(final_order.status.value).inc()
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
async def list_orders(user: User = Depends(get_current_user)) -> list[OrderResponse]:
    stack = _stack_for(user)
    return [_to_response(o) for o in stack.order_manager.list_all(str(user.id))]


@router.post("/orders/{order_id}/cancel", response_model=OrderResponse)
async def cancel_order(
    order_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> OrderResponse:
    stack = _stack_for(user)
    order = stack.order_manager.get(order_id)
    if order is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found")
    if order.status not in (OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED):
        raise HTTPException(status.HTTP_409_CONFLICT, f"Cannot cancel an order in status {order.status.value}")

    if order.broker_order_id:
        await stack.broker.cancel_order(order.broker_order_id)
    stack.order_manager.transition(order_id, OrderStatus.CANCELLED, "cancelled by user")
    final_order = stack.order_manager.get(order_id)
    ORDER_COUNT.labels(final_order.status.value).inc()
    await record_audit(db, actor="user", action="order.cancelled", user_id=user.id, details={"order_id": str(order_id)})
    await _publish_order_event(user, final_order)
    return _to_response(final_order)
