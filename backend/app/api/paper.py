import uuid
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.orders import _get_instrument_by_symbol
from app.auth.dependencies import get_current_user
from app.core.audit import record_audit
from app.database.models.notifications import NotificationType
from app.database.models.risk import RiskDecision as RiskEventDecision
from app.database.models.risk import RiskEvent
from app.database.models.strategy import Direction
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.trading import ExecutionMode
from app.database.models.trading import Trade as TradeRow
from app.database.models.users import User
from app.database.session import get_db
from app.notifications.service import create_notification
from app.paper.engine import PaperTradingEngine
from app.smc.types import Candle
from app.strategy.dsl import StrategyDefinition
from app.trading.persistence import persist_position

router = APIRouter(prefix="/paper", tags=["paper"])


@dataclass
class _PaperSession:
    """Wraps the in-memory engine with the identity `app.trading.persistence`
    needs to journal a `Trade` row on close — the engine itself only knows
    a plain `symbol` string, not a real `instruments` row, strategy, or
    version."""

    engine: PaperTradingEngine
    instrument_id: uuid.UUID
    strategy_id: uuid.UUID
    strategy_version: int
    opened_at: datetime | None = None
    open_snapshot: dict | None = None


# In-memory session registry — see the note in app/api/replay.py.
_SESSIONS: dict[uuid.UUID, _PaperSession] = {}


def _get_owned_session(session_id: uuid.UUID, user: User) -> _PaperSession:
    """`PaperTradingEngine.account_id` is set to the creating user's id at
    `create_paper_session` and never changes — no other user's calls
    should ever be able to look this session up, feed candles into it, or
    even confirm it exists. A caller who isn't the owner gets the same
    404 as a session that doesn't exist, never a 403 that would leak
    which one is true."""
    session = _SESSIONS.get(session_id)
    if session is None or session.engine.account_id != str(user.id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Paper trading session not found")
    return session


class CreatePaperSessionRequest(BaseModel):
    strategy_id: uuid.UUID
    symbol: str
    starting_balance: float = 100_000.0


class PaperStateResponse(BaseModel):
    session_id: uuid.UUID
    balance: float
    equity: float
    open_position: dict | None
    trades_today: int


async def _state_response(session_id: uuid.UUID, engine: PaperTradingEngine) -> PaperStateResponse:
    account = await engine.broker.get_account()
    position = engine.position_manager.get(engine.account_id, engine.symbol)
    return PaperStateResponse(
        session_id=session_id,
        balance=account.balance,
        equity=account.equity,
        open_position=(
            {
                "quantity": position.quantity,
                "average_price": position.average_price,
                "unrealized_pnl": position.unrealized_pnl,
                "stop": position.stop,
                "target": position.target,
            }
            if position and position.is_open
            else None
        ),
        trades_today=engine.trades_today,
    )


@router.post("", response_model=PaperStateResponse)
async def create_paper_session(
    payload: CreatePaperSessionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaperStateResponse:
    strategy_row = await db.get(StrategyRow, payload.strategy_id)
    if strategy_row is None or strategy_row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    strategy = StrategyDefinition.model_validate(strategy_row.definition)
    instrument = await _get_instrument_by_symbol(db, payload.symbol)

    session_id = uuid.uuid4()
    engine = PaperTradingEngine(
        strategy, symbol=payload.symbol, account_id=str(user.id), starting_balance=payload.starting_balance
    )
    _SESSIONS[session_id] = _PaperSession(
        engine=engine, instrument_id=instrument.id, strategy_id=strategy_row.id, strategy_version=strategy_row.version
    )
    return await _state_response(session_id, engine)


@router.get("/{session_id}", response_model=PaperStateResponse)
async def get_paper_session(session_id: uuid.UUID, user: User = Depends(get_current_user)) -> PaperStateResponse:
    return await _state_response(session_id, _get_owned_session(session_id, user).engine)


class FeedCandleRequest(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@router.post("/{session_id}/candle", response_model=PaperStateResponse)
async def feed_candle(
    session_id: uuid.UUID, payload: FeedCandleRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PaperStateResponse:
    """Blueprint §49/§61: a manual paper trading session is a rehearsal of
    a real account, not a toy — closing a position here has to leave the
    same kind of trail a live or autonomous trade does. Before this, only
    `AutoTradeSupervisor` (driving this exact same `PaperTradingEngine`)
    ever wrote a `Trade` journal row or a notification on close; a manual
    session's entire history vanished the moment it was deleted or the
    process restarted, with nothing queryable anywhere.
    """
    session = _get_owned_session(session_id, user)
    engine = session.engine
    candle = Candle(**payload.model_dump())

    position_before = engine.position_manager.get(engine.account_id, engine.symbol)
    if position_before is not None and position_before.is_open:
        session.open_snapshot = {
            "direction": Direction.LONG if position_before.is_long else Direction.SHORT,
            "quantity": abs(position_before.quantity),
            "entry_price": position_before.average_price,
            "stop": position_before.stop,
            "target": position_before.target,
        }

    outcome = await engine.on_candle(candle, db)

    # Blueprint §9/§86: `persist_position` is how a live/MockBroker order
    # placed through POST /orders or /options/execute mirrors its position
    # into the `positions` table so GET /portfolio, GET /admin/
    # portfolio-snapshot, and the correlated-exposure risk check can see
    # it -- this engine (the same PaperTradingEngine AutoTradeSupervisor
    # drives) never called it at all, so a manual paper session's open
    # position, however large, was invisible to every one of those until
    # the moment it closed and a Trade row appeared. Persisted on every
    # candle (not just open/close) so mark-to-market unrealized_pnl stays
    # current too, and `is_open` flips to false the instant a position
    # actually closes.
    position_after = engine.position_manager.get(engine.account_id, engine.symbol)
    if position_after is not None:
        await persist_position(db, user.id, session.instrument_id, position_after, execution_mode=ExecutionMode.PAPER)

    if outcome.risk_checks is not None:
        # Blueprint's `risk_events` table (see AI_TRADING_PLATFORM_BLUEPRINT.md)
        # is the only queryable audit trail of what the risk engine actually
        # decided -- POST /orders and POST /options/execute both write this
        # row unconditionally (approve or reject); paper trading evaluates the
        # exact same RiskEngine but, before this, never wrote one at all.
        db.add(
            RiskEvent(
                user_id=user.id,
                decision=RiskEventDecision.REJECT if outcome.risk_rejected_reason is not None else RiskEventDecision.APPROVE,
                reason=outcome.risk_rejected_reason,
                checks=outcome.risk_checks,
            )
        )
        await db.commit()

    if outcome.order_created:
        session.opened_at = candle.timestamp
        direction = outcome.signal.direction if outcome.signal else None
        await record_audit(
            db,
            actor="user",
            action="paper.order_placed",
            user_id=user.id,
            details={"symbol": engine.symbol, "direction": direction},
        )

        # Blueprint §63 mandates a "Trade executed" notification -- the
        # sibling risk_rejected_reason/closed_position_pnl branches below
        # both notify, but this one, the actual open of a position, never
        # did: NotificationType.TRADE_EXECUTED was defined but nothing in
        # the codebase ever passed it to create_notification.
        await create_notification(
            db,
            user_id=user.id,
            notification_type=NotificationType.TRADE_EXECUTED,
            title=f"{engine.symbol} paper trade executed",
            body=f"Opened {direction or 'a'} position in {engine.symbol}",
            data={"symbol": engine.symbol, "direction": direction},
        )

    if outcome.risk_rejected_reason is not None:
        # Blueprint §63 mandates an "Order rejected" notification. Before
        # this, a matched entry signal the risk engine blocked (daily loss
        # limit, max positions, correlated exposure, ...) left literally no
        # trace anywhere in a paper session: not a notification, not an
        # audit entry, not even a field on `PaperStateResponse` -- the
        # rejection vanished the instant `on_candle` returned.
        await record_audit(
            db,
            actor="user",
            action="paper.order_rejected",
            user_id=user.id,
            details={"symbol": engine.symbol, "reason": outcome.risk_rejected_reason},
        )
        # Blueprint §63 lists "Daily loss limit" as its own notification
        # event, distinct from a generic order rejection -- `RiskEngine`
        # already names the specific check that failed
        # (`risk_failed_check`); only fall back to the generic type for
        # every other kind of veto (max positions, correlated exposure,
        # stale data, ...).
        rejection_notification_type = (
            NotificationType.DAILY_LOSS_LIMIT
            if outcome.risk_failed_check == "daily_loss_limit"
            else NotificationType.ORDER_REJECTED
        )
        await create_notification(
            db,
            user_id=user.id,
            notification_type=rejection_notification_type,
            title=f"{engine.symbol} paper trade rejected",
            body=outcome.risk_rejected_reason,
            data={"symbol": engine.symbol, "reason": outcome.risk_rejected_reason},
        )

    if outcome.closed_position_pnl is not None and session.open_snapshot is not None:
        snapshot = session.open_snapshot
        opened_at = session.opened_at or candle.timestamp
        risk_per_unit = abs(snapshot["entry_price"] - snapshot["stop"]) if snapshot["stop"] else None
        r_multiple = (outcome.closed_position_pnl / snapshot["quantity"]) / risk_per_unit if risk_per_unit else None

        db.add(
            TradeRow(
                user_id=user.id,
                instrument_id=session.instrument_id,
                strategy_id=session.strategy_id,
                strategy_version=session.strategy_version,
                execution_mode=ExecutionMode.PAPER,
                direction=snapshot["direction"],
                entry_price=snapshot["entry_price"],
                exit_price=outcome.exit_price,
                quantity=snapshot["quantity"],
                stop=snapshot["stop"],
                target=snapshot["target"],
                pnl=outcome.closed_position_pnl,
                r_multiple=r_multiple,
                opened_at=opened_at,
                closed_at=candle.timestamp,
                journal={"source": "manual_paper", "symbol": engine.symbol},
            )
        )
        await db.commit()
        # Blueprint §63 lists SL/TP hits as their own notification events,
        # distinct from a generic "position closed" -- `_maybe_exit`
        # already knows which bracket side fired; falls back to the
        # generic type only if a position closed some other way this
        # engine doesn't track (there currently isn't one, but nothing
        # here should assume that stays true forever).
        notification_type = {
            "stop_loss": NotificationType.SL_HIT,
            "take_profit": NotificationType.TP_HIT,
        }.get(outcome.exit_reason, NotificationType.POSITION_CLOSED)
        await create_notification(
            db,
            user_id=user.id,
            notification_type=notification_type,
            title=f"{engine.symbol} paper trade closed",
            body=f"Realized P&L: {outcome.closed_position_pnl:.2f}",
            data={"symbol": engine.symbol, "pnl": outcome.closed_position_pnl, "exit_reason": outcome.exit_reason},
        )
        session.open_snapshot = None
        session.opened_at = None

    return await _state_response(session_id, engine)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def close_paper_session(session_id: uuid.UUID, user: User = Depends(get_current_user)) -> None:
    """Ends a paper trading session, freeing its in-memory engine.
    `_SESSIONS` has no automatic eviction — every created session stays in
    process memory until this is called or the process restarts. Closed
    trades are already journaled (see `feed_candle`) by the time this
    runs; only the live working copy and any still-open position's
    unrealized state are discarded."""
    _get_owned_session(session_id, user)
    del _SESSIONS[session_id]
