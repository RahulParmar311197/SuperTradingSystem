import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    InvalidTokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token_payload,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.config import get_settings
from app.database.models.users import User, UserSession
from app.users.service import create_user, get_user_by_email, get_user_by_id

settings = get_settings()


class AuthError(Exception):
    pass


async def register(db: AsyncSession, email: str, password: str, name: str) -> User:
    existing = await get_user_by_email(db, email)
    if existing is not None:
        raise AuthError("An account with this email already exists")
    user = await create_user(db, email=email, password_hash=hash_password(password), name=name)
    await db.commit()
    return user


async def _issue_tokens(db: AsyncSession, user: User, device_info: str | None = None) -> tuple[str, str]:
    session_id = uuid.uuid4()
    refresh_token = create_refresh_token(user.id, session_id)
    expires_at = datetime.now(timezone.utc) + timedelta(days=settings.refresh_token_expire_days)
    db.add(
        UserSession(
            id=session_id,
            user_id=user.id,
            refresh_token_hash=hash_token(refresh_token),
            device_info=device_info,
            expires_at=expires_at,
        )
    )
    access_token = create_access_token(user.id)
    await db.commit()
    return access_token, refresh_token


async def login(
    db: AsyncSession, email: str, password: str, device_info: str | None = None
) -> tuple[str, str]:
    user = await get_user_by_email(db, email)
    if user is None or not verify_password(password, user.password_hash):
        raise AuthError("Invalid email or password")
    return await _issue_tokens(db, user, device_info=device_info)


async def refresh(db: AsyncSession, refresh_token: str) -> tuple[str, str]:
    try:
        payload = decode_token_payload(refresh_token, TokenType.REFRESH)
    except InvalidTokenError as exc:
        raise AuthError("Invalid refresh token") from exc

    session_id = payload.get("sid")
    user_id = payload.get("sub")
    if not session_id or not user_id:
        raise AuthError("Invalid refresh token")

    result = await db.execute(select(UserSession).where(UserSession.id == uuid.UUID(session_id)))
    session = result.scalar_one_or_none()
    if (
        session is None
        or session.revoked
        or session.refresh_token_hash != hash_token(refresh_token)
        or session.expires_at < datetime.now(timezone.utc)
    ):
        raise AuthError("Refresh token is no longer valid")

    user = await get_user_by_id(db, uuid.UUID(user_id))
    if user is None:
        raise AuthError("User not found")

    # Rotate: revoke the used refresh token and issue a new pair.
    session.revoked = True
    return await _issue_tokens(db, user, device_info=session.device_info)
