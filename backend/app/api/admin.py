"""Admin dashboard (blueprint §116): read-only monitoring across every
account — users, broker connections, orders, and risk events — gated on
the `ADMIN` role (§115). Never exposes `encrypted_credentials`; blueprint
§116 is explicit that "Admin should NOT casually have access to users'
broker secrets."
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.core.audit import record_audit
from app.core.redis import (
    account_halt_reason,
    clear_account_kill,
    clear_global_kill,
    clear_strategy_kill,
    is_global_killed,
    list_halted_accounts,
    list_killed_accounts,
    list_killed_strategies,
    resume_account,
    set_account_kill,
    set_global_kill,
    set_strategy_kill,
)
from app.database.models.ai import AIDecision, AIDecisionType
from app.database.models.instruments import Instrument
from app.database.models.risk import RiskDecision, RiskEvent
from app.database.models.trading import Order, OrderStatus
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User, UserRole, UserStatus
from app.database.session import get_db
from app.market.backfill import backfill_candles
from app.market.providers.factory import market_data_provider_or_reason
from app.market.providers.instrument_master import InstrumentKeyUnknown
from app.options.ingestion import ChainSpotPriceUnavailable, ingest_option_chain
from app.monitoring.health import ComponentStatus, check_database, check_redis, check_workers
from app.trading.portfolio_snapshots import snapshot_all_stacks

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminUserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    status: UserStatus
    trading_permissions: list[str]
    auto_trading_enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/users", response_model=list[AdminUserResponse])
async def list_users(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[User]:
    stmt = select(User).order_by(User.created_at.desc()).limit(limit)
    return (await db.execute(stmt)).scalars().all()


class AdminBrokerConnectionResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    broker: BrokerName
    status: BrokerAccountStatus
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/broker-connections", response_model=list[AdminBrokerConnectionResponse])
async def list_broker_connections(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[BrokerAccount]:
    # Selecting explicit columns (not the ORM row) keeps
    # `encrypted_credentials` from ever entering this response, even if a
    # future field gets added to AdminBrokerConnectionResponse carelessly.
    stmt = (
        select(BrokerAccount.id, BrokerAccount.user_id, BrokerAccount.broker, BrokerAccount.status, BrokerAccount.created_at)
        .order_by(BrokerAccount.created_at.desc())
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [
        AdminBrokerConnectionResponse(id=r.id, user_id=r.user_id, broker=r.broker, status=r.status, created_at=r.created_at)
        for r in rows
    ]


class AdminOrderResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    instrument_id: uuid.UUID
    status: OrderStatus
    quantity: float
    rejection_reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/orders", response_model=list[AdminOrderResponse])
async def list_orders(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[Order]:
    stmt = select(Order).order_by(Order.created_at.desc()).limit(limit)
    return (await db.execute(stmt)).scalars().all()


class AdminRiskEventResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    decision: RiskDecision
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/risk-events", response_model=list[AdminRiskEventResponse])
async def list_risk_events(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[RiskEvent]:
    stmt = select(RiskEvent).order_by(RiskEvent.created_at.desc()).limit(limit)
    return (await db.execute(stmt)).scalars().all()


class AdminAIDecisionResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    decision_type: AIDecisionType
    validated: bool
    validation_errors: list
    model: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


@router.get("/ai-decisions", response_model=list[AdminAIDecisionResponse])
async def list_ai_decisions(
    limit: int = Query(default=100, ge=1, le=500),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[AIDecision]:
    """Blueprint §71 audit logging ("AI decision" is explicitly listed)
    and §79 "AI Model Evaluation" — neither is possible without a record
    of what the AI actually said, which `POST /ai/propose-trade` now
    persists (see `app.database.models.ai.AIDecision`)."""
    stmt = select(AIDecision).order_by(AIDecision.created_at.desc()).limit(limit)
    return (await db.execute(stmt)).scalars().all()


class AdminSystemHealthResponse(BaseModel):
    database: ComponentStatus
    redis: ComponentStatus
    workers: dict[str, str]
    total_users: int
    active_broker_connections: int


@router.get("/system-health", response_model=AdminSystemHealthResponse)
async def system_health(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> AdminSystemHealthResponse:
    """Blueprint §117 "System Health" from the admin's vantage point —
    component health plus the account-level numbers only an admin should
    see in one place."""
    total_users = (await db.execute(select(User.id))).scalars().all()
    active_connections = (
        await db.execute(select(BrokerAccount.id).where(BrokerAccount.status == BrokerAccountStatus.ACTIVE))
    ).scalars().all()
    return AdminSystemHealthResponse(
        database=await check_database(),
        redis=await check_redis(),
        workers=await check_workers(),
        total_users=len(total_users),
        active_broker_connections=len(active_connections),
    )


class HaltedAccountResponse(BaseModel):
    account_id: str
    reason: str


@router.get("/halted-accounts", response_model=list[HaltedAccountResponse])
async def get_halted_accounts(user: User = Depends(require_admin)) -> list[HaltedAccountResponse]:
    """Blueprint §116 "Trading status": which accounts are currently
    blocked from new entries and why — previously only discoverable by
    an affected user hitting a 423 on `POST /orders`, or by reading Redis
    directly."""
    halted = await list_halted_accounts()
    return [HaltedAccountResponse(account_id=account_id, reason=reason) for account_id, reason in halted.items()]


class ResumeAccountRequest(BaseModel):
    confirm: bool = Field(description="Must be true — resuming a halted account requires explicit confirmation")


@router.post("/accounts/{account_id}/resume", response_model=HaltedAccountResponse)
async def resume_halted_account(
    account_id: str,
    payload: ResumeAccountRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> HaltedAccountResponse:
    """Blueprint §75: "Resuming is deliberate manual step, not automatic."
    This is that step — previously nonexistent: `app.core.redis.resume_account`
    was fully implemented but nothing in the API ever called it, so a
    reconciliation-triggered halt had no way to be lifted short of
    editing Redis by hand."""
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm=true to resume this account")

    reason = await account_halt_reason(account_id)
    if reason is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Account {account_id} is not currently halted")

    await resume_account(account_id)
    await record_audit(
        db,
        actor="user",
        action="admin.account_resumed",
        user_id=user.id,
        details={"resumed_account_id": account_id, "previous_halt_reason": reason},
    )
    return HaltedAccountResponse(account_id=account_id, reason=reason)


class PortfolioSnapshotTriggerResponse(BaseModel):
    accounts_snapshotted: int


@router.post("/portfolio-snapshot", response_model=PortfolioSnapshotTriggerResponse)
async def trigger_portfolio_snapshot(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> PortfolioSnapshotTriggerResponse:
    """Blueprint §9 `portfolio_snapshots`: journals one row per account
    with an open position right now (balance/equity/exposure/Greeks),
    previously a schema-only table with zero writers. On-demand rather
    than an automatic loop — see `app.trading.portfolio_snapshots`'s
    module docstring for why a background loop was tried and dropped; a
    real deployment should call this from an external scheduler."""
    count = await snapshot_all_stacks()
    await record_audit(db, actor="user", action="admin.portfolio_snapshot_triggered", user_id=user.id, details={"accounts_snapshotted": count})
    return PortfolioSnapshotTriggerResponse(accounts_snapshotted=count)


class KillSwitchStateResponse(BaseModel):
    global_kill: bool
    killed_accounts: list[str]
    killed_strategies: list[str]


async def _kill_switch_state() -> KillSwitchStateResponse:
    return KillSwitchStateResponse(
        global_kill=await is_global_killed(),
        killed_accounts=await list_killed_accounts(),
        killed_strategies=await list_killed_strategies(),
    )


@router.get("/kill-switch", response_model=KillSwitchStateResponse)
async def get_kill_switch(user: User = Depends(require_admin)) -> KillSwitchStateResponse:
    """Blueprint §58 "Kill switch": three levels (global/account/strategy)
    that `RiskEngine.evaluate`/`evaluate_options_risk` already check on
    every proposal via `KillSwitchState.is_blocked` -- but `KillSwitchState`
    was a plain in-memory dataclass that nothing anywhere ever called
    `kill_global`/`kill_account`/`kill_strategy` on, so the check was
    permanently a no-op in every environment regardless of operator intent.
    Backed by Redis (app.core.redis) the same way account halts already
    are, so a kill triggered here is visible to every RiskEngine in every
    process on its very next evaluation (see
    app.risk.kill_switch.load_kill_switch_state)."""
    return await _kill_switch_state()


class KillSwitchConfirmRequest(BaseModel):
    confirm: bool = Field(description="Must be true — triggering or clearing a kill switch requires explicit confirmation")


@router.post("/kill-switch/global", response_model=KillSwitchStateResponse)
async def trigger_global_kill_switch(
    payload: KillSwitchConfirmRequest, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> KillSwitchStateResponse:
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm=true to trigger the global kill switch")
    await set_global_kill()
    await record_audit(db, actor="user", action="admin.kill_switch_global_triggered", user_id=user.id, details={})
    return await _kill_switch_state()


@router.delete("/kill-switch/global", response_model=KillSwitchStateResponse)
async def clear_global_kill_switch(user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> KillSwitchStateResponse:
    await clear_global_kill()
    await record_audit(db, actor="user", action="admin.kill_switch_global_cleared", user_id=user.id, details={})
    return await _kill_switch_state()


@router.post("/kill-switch/account/{account_id}", response_model=KillSwitchStateResponse)
async def trigger_account_kill_switch(
    account_id: str,
    payload: KillSwitchConfirmRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> KillSwitchStateResponse:
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm=true to stop trading for this account")
    await set_account_kill(account_id)
    await record_audit(
        db, actor="user", action="admin.kill_switch_account_triggered", user_id=user.id, details={"account_id": account_id}
    )
    return await _kill_switch_state()


@router.delete("/kill-switch/account/{account_id}", response_model=KillSwitchStateResponse)
async def clear_account_kill_switch(
    account_id: str, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> KillSwitchStateResponse:
    await clear_account_kill(account_id)
    await record_audit(
        db, actor="user", action="admin.kill_switch_account_cleared", user_id=user.id, details={"account_id": account_id}
    )
    return await _kill_switch_state()


@router.post("/kill-switch/strategy/{strategy_id}", response_model=KillSwitchStateResponse)
async def trigger_strategy_kill_switch(
    strategy_id: str,
    payload: KillSwitchConfirmRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> KillSwitchStateResponse:
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm=true to stop this strategy")
    await set_strategy_kill(strategy_id)
    await record_audit(
        db,
        actor="user",
        action="admin.kill_switch_strategy_triggered",
        user_id=user.id,
        details={"strategy_id": strategy_id},
    )
    return await _kill_switch_state()


@router.delete("/kill-switch/strategy/{strategy_id}", response_model=KillSwitchStateResponse)
async def clear_strategy_kill_switch(
    strategy_id: str, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
) -> KillSwitchStateResponse:
    await clear_strategy_kill(strategy_id)
    await record_audit(
        db, actor="user", action="admin.kill_switch_strategy_cleared", user_id=user.id, details={"strategy_id": strategy_id}
    )
    return await _kill_switch_state()


class BackfillRequest(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=64)
    timeframe: str = Field(..., min_length=1, max_length=8)
    from_date: date
    to_date: date


class BackfillResponse(BaseModel):
    symbol: str
    timeframe: str
    candles_written: int
    first_timestamp: datetime | None = None
    last_timestamp: datetime | None = None


@router.post("/backfill", response_model=BackfillResponse)
async def backfill_instrument_candles(
    payload: BackfillRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> BackfillResponse:
    """Load an instrument's real price history from the market-data provider.

    **This is the first caller `backfill_candles` has ever had.** The
    function, the read-only `UpstoxMarketData` client and the
    instrument-key resolution it depends on all existed; nothing in `app/`
    invoked any of them, so the only candles a deployment could hold were
    whatever a test had inserted. Everything downstream -- `ScannerWorker`,
    `AutoTradeSupervisor`, the backtest engine, the paper engine's own feed
    -- reads `candles` from Postgres and does not care where a bar came
    from, which is exactly why one missing caller emptied all of them.

    On-demand and admin-gated rather than a background loop, for the same
    reason `POST /admin/portfolio-snapshot` is: the range to fetch is a
    judgement call, provider history is rate-limited, and a real deployment
    should drive this from its own scheduler. Idempotent, because
    `upsert_candles` is a real upsert -- re-running an overlapping range
    corrects bars rather than duplicating them.
    """
    if payload.from_date > payload.to_date:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "from_date must not be after to_date")

    market_data, reason = market_data_provider_or_reason()
    if market_data is None:
        # 503, not 500: the deployment is misconfigured, not broken, and
        # the message names the remedy rather than leaving an operator to
        # read the traceback.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, reason)

    instrument = (
        await db.execute(select(Instrument).where(Instrument.symbol == payload.symbol))
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No instrument registered for {payload.symbol!r}")

    try:
        result = await backfill_candles(
            db, market_data, instrument, payload.timeframe, payload.from_date, payload.to_date
        )
    except InstrumentKeyUnknown as exc:
        # The instrument exists but carries no provider key: a 400 naming
        # the instrument, not a 500 several layers from the cause.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ValueError as exc:
        # `upstox_interval_for` rejects a timeframe the provider does not
        # serve, and says which ones it does.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    await record_audit(
        db,
        actor="user",
        action="admin.candles_backfilled",
        user_id=user.id,
        details={
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "candles_written": result.candles_written,
            "from_date": payload.from_date.isoformat(),
            "to_date": payload.to_date.isoformat(),
        },
    )
    await db.commit()
    return BackfillResponse(
        symbol=result.symbol,
        timeframe=result.timeframe,
        candles_written=result.candles_written,
        first_timestamp=result.first_timestamp,
        last_timestamp=result.last_timestamp,
    )


class OptionChainRequest(BaseModel):
    # The *underlying's* symbol (an index or a stock), not a contract's.
    underlying: str = Field(min_length=1, max_length=64)
    expiry: date


class OptionChainResponse(BaseModel):
    underlying: str
    expiry: date
    fetched_at: datetime
    spot_price: float
    quotes_returned: int
    snapshots_written: int
    unregistered_count: int
    unregistered_sample: list[str]


@router.post("/option-chain", response_model=OptionChainResponse)
async def ingest_underlying_option_chain(
    payload: OptionChainRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> OptionChainResponse:
    """Load one underlying's option chain for one expiry.

    **This is the first writer `option_snapshots` has ever had**, and with
    it the three options-specific gates on `POST /options/execute` become
    real: liquidity (volume, open interest, spread), premium deviation
    against the actual bid/ask mid, and quote staleness. Until now each
    recorded `None` -- correctly, since nothing had looked -- so an
    operator could execute a multi-leg strategy against a contract nobody
    had ever quoted. `app/trading/portfolio_snapshots.py` also stops
    contributing 0 for every option position's Greeks.

    Admin-gated and on demand, like `POST /admin/backfill`: which expiry
    to fetch is a judgement call and provider chains are rate-limited.

    Not idempotent, deliberately. Each run appends a new chain with fresh
    snapshots, because a snapshot is a timestamped quote -- and the
    staleness gate downstream only means something if the store keeps
    *when* each quote was taken.
    """
    market_data, reason = market_data_provider_or_reason()
    if market_data is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, reason)

    instrument = (
        await db.execute(select(Instrument).where(Instrument.symbol == payload.underlying))
    ).scalar_one_or_none()
    if instrument is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"No instrument registered for {payload.underlying!r}"
        )

    try:
        result = await ingest_option_chain(db, market_data, instrument, payload.expiry)
    except InstrumentKeyUnknown as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    except ChainSpotPriceUnavailable as exc:
        # 502: the request was well formed and the provider answered, but
        # with something this system will not store. Distinct from the 400
        # above, which is this deployment's own misconfiguration.
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    await record_audit(
        db,
        actor="user",
        action="admin.option_chain_ingested",
        user_id=user.id,
        details={
            "underlying": result.underlying,
            "expiry": result.expiry.isoformat(),
            "snapshots_written": result.snapshots_written,
            "quotes_returned": result.quotes_returned,
            "unregistered_count": result.unregistered_count,
        },
    )
    await db.commit()
    return OptionChainResponse(
        underlying=result.underlying,
        expiry=result.expiry,
        fetched_at=result.fetched_at,
        spot_price=result.spot_price,
        quotes_returned=result.quotes_returned,
        snapshots_written=result.snapshots_written,
        unregistered_count=result.unregistered_count,
        unregistered_sample=result.unregistered_sample,
    )
