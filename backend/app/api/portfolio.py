from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.orders import _execution_mode_for, _mark_open_positions_to_market, _stack_for
from app.auth.dependencies import get_current_user
from app.database.models.users import User
from app.database.session import get_db
from app.risk.portfolio import compute_portfolio_exposure

router = APIRouter(tags=["portfolio"])


class PortfolioResponse(BaseModel):
    balance: float
    equity: float
    open_position_count: int
    total_unrealized_pnl: float
    total_realized_pnl: float
    # Blueprint §86 "Portfolio Risk": total exposure and market-type
    # breakdown, read from the persisted `positions` table (see
    # app.trading.persistence) rather than the in-memory position
    # manager above — this is what a restart, an admin, or another
    # process would also see.
    total_exposure: float
    exposure_by_market: dict[str, float]


@router.get("/portfolio", response_model=PortfolioResponse)
async def get_portfolio(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> PortfolioResponse:
    stack = await _stack_for(user, db)
    account = await stack.broker.get_account()
    positions = await _mark_open_positions_to_market(stack, str(user.id))
    # Positions are persisted under whichever execution_mode actually
    # produced them (blueprint §101) -- a user with no connected broker
    # trades PAPER against MockBroker, and querying the LIVE default here
    # would silently report zero exposure for every such account.
    exposure = await compute_portfolio_exposure(db, user.id, execution_mode=_execution_mode_for(stack))
    return PortfolioResponse(
        balance=account.balance,
        equity=account.equity,
        open_position_count=len(positions),
        total_unrealized_pnl=sum(p.unrealized_pnl for p in positions),
        # Realized P&L comes from the persisted `positions` rows, not from
        # `positions` above -- that list is `open_positions()`, and realized
        # P&L only exists because a position *closed*. Summing it over open
        # positions reported 0.0 for every fully closed trade, and for a
        # partial close reported only what had been realized so far, so
        # closing the remainder drove the figure back down to zero. The DB
        # is also the same source `total_exposure` below already uses, and
        # unlike the in-memory manager it survives a process restart.
        total_realized_pnl=exposure.total_realized_pnl,
        total_exposure=exposure.total_exposure,
        exposure_by_market=exposure.exposure_by_market,
    )
