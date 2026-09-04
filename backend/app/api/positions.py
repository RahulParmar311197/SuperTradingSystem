from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.orders import _stack_for
from app.auth.dependencies import get_current_user
from app.database.models.users import User

router = APIRouter(tags=["trading"])


class PositionResponse(BaseModel):
    symbol: str
    quantity: float
    average_price: float
    unrealized_pnl: float
    realized_pnl: float


@router.get("/positions", response_model=list[PositionResponse])
async def list_positions(user: User = Depends(get_current_user)) -> list[PositionResponse]:
    stack = _stack_for(user)
    positions = stack.position_manager.open_positions(str(user.id))
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
