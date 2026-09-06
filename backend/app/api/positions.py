from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.orders import _mark_open_positions_to_market, _stack_for
from app.auth.dependencies import get_current_user
from app.database.models.users import User
from app.database.session import get_db

router = APIRouter(tags=["trading"])


class PositionResponse(BaseModel):
    symbol: str
    quantity: float
    average_price: float
    unrealized_pnl: float
    realized_pnl: float


@router.get("/positions", response_model=list[PositionResponse])
async def list_positions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> list[PositionResponse]:
    stack = await _stack_for(user, db)
    positions = await _mark_open_positions_to_market(stack, str(user.id))
    return [
        PositionResponse(
            symbol=p.symbol,
            quantity=p.quantity,
            average_price=p.average_price,
            unrealized_pnl=p.unrealized_pnl,
            realized_pnl=p.realized_pnl,
        )
        for p in positions
    ]
