"""Admin dashboard (blueprint §116): read-only monitoring across every
account — users, broker connections, orders, and risk events — gated on
the `ADMIN` role (§115). Never exposes `encrypted_credentials`; blueprint
§116 is explicit that "Admin should NOT casually have access to users'
broker secrets."
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.core.audit import record_audit
from app.core.redis import account_halt_reason, list_halted_accounts, resume_account
from app.database.models.ai import AIDecision, AIDecisionType
from app.database.models.risk import RiskDecision, RiskEvent
from app.database.models.trading import Order, OrderStatus
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User, UserRole, UserStatus
from app.database.session import get_db
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
    limit: int = Query(default=100, le=500),
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
    limit: int = Query(default=100, le=500),
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
    limit: int = Query(default=100, le=500),
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
    limit: int = Query(default=100, le=500),
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
    limit: int = Query(default=100, le=500),
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
