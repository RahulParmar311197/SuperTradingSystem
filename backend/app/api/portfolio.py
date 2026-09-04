from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.api.orders import _stack_for
from app.auth.dependencies import get_current_user
from app.database.models.users import User

router = APIRouter(tags=["portfolio"])


class PortfolioResponse(BaseModel):
    balance: float
    equity: float
    open_position_count: int
    total_unrealized_pnl: float
    total_realized_pnl: float


@router.get("/portfolio", response_model=PortfolioResponse)
async def get_portfolio(user: User = Depends(get_current_user)) -> PortfolioResponse:
    stack = _stack_for(user)
    account = await stack.broker.get_account()
    positions = stack.position_manager.open_positions(str(user.id))
    return PortfolioResponse(
        balance=account.balance,
        equity=account.equity,
        open_position_count=len(positions),
        total_unrealized_pnl=sum(p.unrealized_pnl for p in positions),
        total_realized_pnl=sum(p.realized_pnl for p in positions),
    )
