import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import service as auth_service
from app.auth.security import InvalidTokenError, TokenType, decode_token_payload
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
        payload = decode_token_payload(credentials.credentials, TokenType.ACCESS)
    except InvalidTokenError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired access token") from exc

    # The session this token was issued from must still be live. Skipping
    # this is what made every §69 revocation path advisory: `logout`,
    # `revoke_session` and the reuse-containment in `refresh` all set
    # `UserSession.revoked`, and this dependency -- the gate in front of
    # every authenticated REST endpoint -- never looked at it, so a
    # revoked session's access token kept working until it expired on its
    # own. A token with no `sid` at all (issued before access tokens
    # carried one) is refused rather than trusted: there is no session to
    # check, so it cannot be revoked, and failing closed costs those
    # holders one re-login.
    session_id = payload.get("sid")
    if not session_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Access token is not bound to a session")
    if await auth_service.get_active_session(db, uuid.UUID(session_id)) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session has been revoked or has expired")

    user = await get_user_by_id(db, uuid.UUID(payload["sub"]))
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
