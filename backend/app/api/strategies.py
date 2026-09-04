import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.database.models.strategy import Strategy as StrategyRow
from app.database.models.users import User
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


@router.post("", response_model=StrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    payload: StrategyDefinition, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> StrategyRow:
    row = StrategyRow(user_id=user.id, name=payload.name, version=1, definition=payload.model_dump(mode="json"))
    db.add(row)
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
    """Updating a strategy bumps its version (blueprint §91) rather than
    overwriting history — a live account should always know exactly which
    version generated a given trade."""
    row = await db.get(StrategyRow, strategy_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Strategy not found")
    row.definition = payload.model_dump(mode="json")
    row.name = payload.name
    row.version += 1
    await db.commit()
    await db.refresh(row)
    return row
