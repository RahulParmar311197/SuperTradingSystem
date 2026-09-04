"""Auto-trading master switch (blueprint §87, §89, §102-103).

Enabling requires the AUTO_TRADE permission *and* an explicit `confirm:
true` in the request body — no implicit or default-on path exists.
Disabling requires neither: it's the "STOP AUTO TRADING" emergency control
that blueprint §103 says must always be available.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user, require_permission
from app.core.audit import record_audit
from app.database.models.users import TradingPermission, User
from app.database.session import get_db

router = APIRouter(prefix="/auto-trading", tags=["auto-trading"])


class AutoTradingStatus(BaseModel):
    enabled: bool
    risk_per_trade_pct: float
    daily_loss_limit_pct: float
    max_trades_per_day: int
    max_positions: int


def _status(user: User) -> AutoTradingStatus:
    return AutoTradingStatus(
        enabled=user.auto_trading_enabled,
        risk_per_trade_pct=user.auto_trading_risk_per_trade_pct,
        daily_loss_limit_pct=user.auto_trading_daily_loss_limit_pct,
        max_trades_per_day=user.auto_trading_max_trades_per_day,
        max_positions=user.auto_trading_max_positions,
    )


@router.get("/status", response_model=AutoTradingStatus)
async def get_status(user: User = Depends(get_current_user)) -> AutoTradingStatus:
    return _status(user)


class EnableAutoTradingRequest(BaseModel):
    confirm: bool = Field(description="Must be true — enabling auto-trading requires explicit confirmation")
    risk_per_trade_pct: float | None = Field(default=None, gt=0, le=100)
    daily_loss_limit_pct: float | None = Field(default=None, gt=0, le=100)
    max_trades_per_day: int | None = Field(default=None, gt=0)
    max_positions: int | None = Field(default=None, gt=0)


@router.post("/enable", response_model=AutoTradingStatus)
async def enable_auto_trading(
    payload: EnableAutoTradingRequest,
    user: User = Depends(require_permission(TradingPermission.AUTO_TRADE)),
    db: AsyncSession = Depends(get_db),
) -> AutoTradingStatus:
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Set confirm=true to enable autonomous trading")

    user.auto_trading_enabled = True
    if payload.risk_per_trade_pct is not None:
        user.auto_trading_risk_per_trade_pct = payload.risk_per_trade_pct
    if payload.daily_loss_limit_pct is not None:
        user.auto_trading_daily_loss_limit_pct = payload.daily_loss_limit_pct
    if payload.max_trades_per_day is not None:
        user.auto_trading_max_trades_per_day = payload.max_trades_per_day
    if payload.max_positions is not None:
        user.auto_trading_max_positions = payload.max_positions

    await db.commit()
    await record_audit(db, actor="user", action="auto_trading.enabled", user_id=user.id, details=_status(user).model_dump())
    return _status(user)


@router.post("/disable", response_model=AutoTradingStatus)
async def disable_auto_trading(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> AutoTradingStatus:
    """No permission check beyond being logged in — a kill switch must
    never be harder to reach than the thing it stops (blueprint §103)."""
    user.auto_trading_enabled = False
    await db.commit()
    await record_audit(db, actor="user", action="auto_trading.disabled", user_id=user.id)
    return _status(user)
