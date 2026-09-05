import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.audit import record_audit
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.strategy import StrategyVersion as StrategyVersionRow
from app.database.models.users import TradingPermission, User
from app.database.session import get_db
from app.strategy.dsl import StrategyDefinition

router = APIRouter(prefix="/strategies", tags=["strategies"])


class StrategyResponse(BaseModel):
    id: uuid.UUID
    name: str
    version: int
    definition: dict
    is_active: bool
    eligible_for_auto_trading: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class StrategyVersionResponse(BaseModel):
    version: int
    name: str
    definition: dict
    created_at: datetime

    model_config = {"from_attributes": True}


async def _snapshot_version(db: AsyncSession, row: StrategyRow) -> None:
    """Write an immutable snapshot of `row`'s current version/definition.
    Called once, at the moment that version comes into existence — never
    to correct or replace an existing snapshot."""
    db.add(StrategyVersionRow(strategy_id=row.id, version=row.version, name=row.name, definition=row.definition))


@router.post("", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    payload: StrategyDefinition, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> StrategyRow:
    row = StrategyRow(user_id=user.id, name=payload.name, version=1, definition=payload.model_dump(mode="json"))
    db.add(row)
    await db.flush()
    await _snapshot_version(db, row)
    await db.commit()
    await db.refresh(row)
    return row


@router.get("", response_model=list[StrategyResponse])
async def list_strategies(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[StrategyRow]:
    stmt = select(StrategyRow).where(StrategyRow.user_id == user.id)
    return (await db.execute(stmt)).scalars().all()


@router.get("/{strategy_id}", response_model=StrategyResponse)
async def get_strategy(
    strategy_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> StrategyRow:
    row = await db.get(StrategyRow, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    return row


@router.put("/{strategy_id}", response_model=StrategyResponse)
async def update_strategy(
    strategy_id: uuid.UUID,
    payload: StrategyDefinition,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StrategyRow:
    """Updating a strategy bumps its version (blueprint §91) and snapshots
    the new definition into `strategy_versions` before committing, so a
    trade's `strategy_version` can always be resolved back to the exact
    DSL that produced it via GET /strategies/{id}/versions/{version} —
    editing the strategy again never loses that snapshot."""
    row = await db.get(StrategyRow, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    row.definition = payload.model_dump(mode="json")
    row.name = payload.name
    row.version += 1
    await _snapshot_version(db, row)
    await db.commit()
    await db.refresh(row)
    return row


class UpdateStrategyStatusRequest(BaseModel):
    is_active: bool | None = None
    eligible_for_auto_trading: bool | None = None
    confirm: bool = Field(
        default=False,
        description="Must be true when setting eligible_for_auto_trading=True -- promoting a strategy to "
        "autonomous trading requires explicit confirmation, the same as POST /auto-trading/enable.",
    )


@router.patch("/{strategy_id}/status", response_model=StrategyResponse)
async def update_strategy_status(
    strategy_id: uuid.UUID,
    payload: UpdateStrategyStatusRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StrategyRow:
    """Blueprint §77: a strategy graduates to `eligible_for_auto_trading`
    only after Backtest -> Out-of-sample -> Replay -> Paper trading -> Risk
    review -- deliberately a separate, explicit step from `update_strategy`
    (editing the DSL) so promoting a strategy to autonomous trading is
    never a side effect of an unrelated edit. Before this endpoint existed,
    nothing in the API ever wrote `eligible_for_auto_trading`, so
    `AutoTradeSupervisor`'s `WHERE eligible_for_auto_trading IS TRUE`
    filter was unsatisfiable for every real user -- autonomous trading
    could never place a single order regardless of the account-level
    `POST /auto-trading/enable` switch.
    """
    row = await db.get(StrategyRow, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")

    if payload.eligible_for_auto_trading is True and row.eligible_for_auto_trading is not True:
        if TradingPermission.AUTO_TRADE.value not in user.trading_permissions:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Missing required trading permission: AUTO_TRADE")
        if not payload.confirm:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm=true to mark a strategy eligible for auto-trading")

    if payload.is_active is not None:
        row.is_active = payload.is_active
    if payload.eligible_for_auto_trading is not None:
        row.eligible_for_auto_trading = payload.eligible_for_auto_trading

    await db.commit()
    await db.refresh(row)
    await record_audit(
        db,
        actor="user",
        action="strategy.status_updated",
        user_id=user.id,
        details={"strategy_id": str(row.id), "is_active": row.is_active, "eligible_for_auto_trading": row.eligible_for_auto_trading},
    )
    return row


@router.get("/{strategy_id}/versions", response_model=list[StrategyVersionResponse])
async def list_strategy_versions(
    strategy_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[StrategyVersionRow]:
    row = await db.get(StrategyRow, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    stmt = select(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id).order_by(StrategyVersionRow.version)
    return (await db.execute(stmt)).scalars().all()


@router.get("/{strategy_id}/versions/{version}", response_model=StrategyVersionResponse)
async def get_strategy_version(
    strategy_id: uuid.UUID, version: int, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> StrategyVersionRow:
    row = await db.get(StrategyRow, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    stmt = select(StrategyVersionRow).where(StrategyVersionRow.strategy_id == strategy_id, StrategyVersionRow.version == version)
    version_row = (await db.execute(stmt)).scalar_one_or_none()
    if version_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy version not found")
    return version_row
