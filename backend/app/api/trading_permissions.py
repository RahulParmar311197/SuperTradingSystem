"""Self-service grant/revoke for the risk-bearing trading permissions
(blueprint §88 "Trading Permission", §89 "Auto-Trading Settings").

`VIEW`, `ANALYZE` and `PAPER_TRADE` are granted to every account at
registration (see `app.users.service.create_user`) — no real risk, no
gate needed. `LIVE_TRADE` and `AUTO_TRADE` are not: a user must explicitly
opt in, mirroring the "require explicit activation" principle blueprint
§101-102 applies to live trading itself, and which
`POST /auto-trading/enable` already enforces via `require_permission`.
Without this endpoint that gate had no way to ever be satisfied — nothing
in the API could add `AUTO_TRADE` (or `LIVE_TRADE`) to a user's
permissions at all.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.audit import record_audit
from app.database.models.users import TradingPermission, User
from app.database.session import get_db

router = APIRouter(prefix="/trading-permissions", tags=["trading-permissions"])

# The only permissions a user can request here — VIEW/ANALYZE/PAPER_TRADE
# are already granted at registration and aren't requestable or
# revocable through this endpoint.
_GRANTABLE = {TradingPermission.LIVE_TRADE, TradingPermission.AUTO_TRADE}


class TradingPermissionsResponse(BaseModel):
    permissions: list[str]


@router.get("", response_model=TradingPermissionsResponse)
async def get_permissions(user: User = Depends(get_current_user)) -> TradingPermissionsResponse:
    return TradingPermissionsResponse(permissions=user.trading_permissions)


class GrantPermissionRequest(BaseModel):
    permission: TradingPermission
    confirm: bool = Field(description="Must be true — granting LIVE_TRADE or AUTO_TRADE requires explicit confirmation")


@router.post("/grant", response_model=TradingPermissionsResponse)
async def grant_permission(
    payload: GrantPermissionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> TradingPermissionsResponse:
    if payload.permission not in _GRANTABLE:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"{payload.permission.value} is granted automatically and cannot be requested here"
        )
    if not payload.confirm:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Set confirm=true to enable {payload.permission.value}")

    if payload.permission.value not in user.trading_permissions:
        user.trading_permissions = [*user.trading_permissions, payload.permission.value]
        await db.commit()
        await record_audit(
            db, actor="user", action="trading_permission.granted", user_id=user.id, details={"permission": payload.permission.value}
        )
    return TradingPermissionsResponse(permissions=user.trading_permissions)


class RevokePermissionRequest(BaseModel):
    permission: TradingPermission


@router.post("/revoke", response_model=TradingPermissionsResponse)
async def revoke_permission(
    payload: RevokePermissionRequest, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> TradingPermissionsResponse:
    """No confirmation required — revoking a risk-bearing permission is a
    kill switch and must never be harder to reach than granting it
    (same principle as `POST /auto-trading/disable`)."""
    if payload.permission.value in user.trading_permissions:
        user.trading_permissions = [p for p in user.trading_permissions if p != payload.permission.value]
        await db.commit()
        await record_audit(
            db, actor="user", action="trading_permission.revoked", user_id=user.id, details={"permission": payload.permission.value}
        )
    return TradingPermissionsResponse(permissions=user.trading_permissions)
