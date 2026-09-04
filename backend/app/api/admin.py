"""Admin dashboard (blueprint §116): read-only monitoring across every
account — users, broker connections, orders, and risk events — gated on
the `ADMIN` role (§115). Never exposes `encrypted_credentials`; blueprint
§116 is explicit that "Admin should NOT casually have access to users'
broker secrets."
"""

from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import require_admin
from app.database.models.risk import RiskDecision, RiskEvent
from app.database.models.trading import Order, OrderStatus
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName, User, UserRole, UserStatus
from app.database.session import get_db
from app.monitoring.health import ComponentStatus, check_database, check_redis, check_workers

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
