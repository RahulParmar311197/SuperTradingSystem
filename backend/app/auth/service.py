import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import (
    DUMMY_PASSWORD_HASH,
    InvalidTokenError,
    TokenType,
    create_access_token,
    create_refresh_token,
    decode_token_payload,
    hash_password,
    hash_token,
    verify_password,
)
from app.core.audit import record_audit
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
    await record_audit(db, actor="user", action="user.registered", user_id=user.id, details={"email": email})
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
    # Always pay bcrypt's cost, even for an email with no account -- see
    # DUMMY_PASSWORD_HASH's docstring in app/auth/security.py. Whether
    # `user` exists must never be observable from response latency.
    password_hash = user.password_hash if user is not None else DUMMY_PASSWORD_HASH
    password_valid = verify_password(password, password_hash)
    if user is None or not password_valid:
        await record_audit(db, actor="user", action="login.failed", details={"email": email})
        raise AuthError("Invalid email or password")
    tokens = await _issue_tokens(db, user, device_info=device_info)
    await record_audit(db, actor="user", action="login.succeeded", user_id=user.id)
    return tokens


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
    token_matches = session is not None and session.refresh_token_hash == hash_token(refresh_token)

    if token_matches and session.revoked:
        # Reuse of a refresh token that already rotated (or was logged
        # out) is the textbook signal that it was stolen: a legitimate
        # client and a thief racing to use the same token, or a thief
        # replaying one whose legitimate owner already moved past it. The
        # whole reason self-service session revocation (blueprint §69)
        # exists is a stolen token getting real remediation -- but that
        # only helps if the legitimate user notices, and this exact,
        # matching, already-used token being presented again was
        # previously treated as an ordinary "invalid token" error with no
        # side effect at all: no revocation of whatever session the
        # rotation produced, no audit trail, nothing. Contain it the same
        # way a user hitting the kill switch would: revoke every active
        # session for this user (not just the one this token names -- the
        # rotated-to session it's not linked to at all) so a thief's live
        # access/refresh pair is cut off too, and log it so this is a
        # visible incident instead of a silent no-op.
        await db.execute(
            update(UserSession).where(UserSession.user_id == session.user_id, UserSession.revoked.is_(False)).values(revoked=True)
        )
        await record_audit(
            db,
            actor="system",
            action="auth.refresh_token_reuse_detected",
            user_id=session.user_id,
            details={"session_id": str(session.id)},
        )
        await db.commit()
        raise AuthError("Refresh token is no longer valid")

    if session is None or session.revoked or not token_matches or session.expires_at < datetime.now(timezone.utc):
        raise AuthError("Refresh token is no longer valid")

    user = await get_user_by_id(db, uuid.UUID(user_id))
    if user is None:
        raise AuthError("User not found")

    # Rotate: revoke the used refresh token and issue a new pair.
    session.revoked = True
    return await _issue_tokens(db, user, device_info=session.device_info)


async def logout(db: AsyncSession, refresh_token: str) -> None:
    """Blueprint §69 "Session management": before this, `UserSession.revoked`
    was only ever set as a side effect of `refresh()` rotating a used
    token -- no user action could revoke a session, so a stolen refresh
    token or a forgotten logged-in shared computer stayed valid until its
    multi-day natural expiry with no self-service remediation. Lenient by
    design (never raises for an already-revoked/rotated-out session, the
    same "a kill switch must never be harder to reach" principle as
    `POST /auto-trading/disable`) -- the caller wanted to be logged out,
    and that's already true.
    """
    try:
        payload = decode_token_payload(refresh_token, TokenType.REFRESH)
    except InvalidTokenError:
        return
    session_id = payload.get("sid")
    if not session_id:
        return

    result = await db.execute(select(UserSession).where(UserSession.id == uuid.UUID(session_id)))
    session = result.scalar_one_or_none()
    if session is not None and not session.revoked:
        session.revoked = True
        await db.commit()
        await record_audit(db, actor="user", action="user.logged_out", user_id=session.user_id)


async def list_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[UserSession]:
    """Blueprint §69 "Device tracking": `device_info` has been collected at
    login since the beginning, but nothing ever read it back -- this is
    the first endpoint that actually surfaces it. Only currently-active
    sessions (not revoked, not yet expired) are worth showing; a long
    history of dead sessions isn't "device tracking", it's noise."""
    result = await db.execute(
        select(UserSession)
        .where(
            UserSession.user_id == user_id,
            UserSession.revoked.is_(False),
            UserSession.expires_at > datetime.now(timezone.utc),
        )
        .order_by(UserSession.created_at.desc())
    )
    return list(result.scalars().all())


async def revoke_session(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    """Returns whether a session owned by `user_id` was found and revoked
    -- the caller turns a `False` into a 404, the same "never confirm
    another user's resource exists" pattern already used for /paper/* and
    /replay/* sessions."""
    result = await db.execute(select(UserSession).where(UserSession.id == session_id))
    session = result.scalar_one_or_none()
    if session is None or session.user_id != user_id:
        return False
    session.revoked = True
    await db.commit()
    await record_audit(
        db, actor="user", action="user.session_revoked", user_id=user_id, details={"session_id": str(session_id)}
    )
    return True
