import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import InvalidTokenError, TokenType, decode_token
from app.database.session import get_db
from app.database.models.users import TradingPermission, User, UserRole, UserStatus
from app.users.service import get_user_by_id

_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    try:
        user_id = decode_token(credentials.credentials, TokenType.ACCESS)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired access token") from exc

    user = await get_user_by_id(db, uuid.UUID(user_id))
    if user is None or user.status != UserStatus.ACTIVE:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User is not active")
    return user


def require_permission(permission: TradingPermission):
    async def _checker(user: User = Depends(get_current_user)) -> User:
        if permission.value not in user.trading_permissions:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, f"Missing required permission: {permission.value}"
            )
        return user

    return _checker


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """Blueprint §115-116: an admin dashboard exists for monitoring, not
    for every authenticated user."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return user
