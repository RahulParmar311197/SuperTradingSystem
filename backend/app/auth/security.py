import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from enum import StrEnum

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings

settings = get_settings()

# bcrypt's underlying algorithm silently ignores any bytes past 72 — reject
# rather than truncate, so two different long passwords can never hash the
# same way. Enforced here (not just at the request-schema layer) because
# this function is the actual security boundary.
_MAX_PASSWORD_BYTES = 72


class TokenType(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class PasswordTooLongError(ValueError):
    pass


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        raise PasswordTooLongError(f"Password must be at most {_MAX_PASSWORD_BYTES} bytes")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode("utf-8")
    if len(encoded) > _MAX_PASSWORD_BYTES:
        return False
    return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))


# A real bcrypt hash with no matching password, computed once at import
# time. `login()` checks against this instead of skipping `verify_password`
# entirely when no account matches the submitted email — bcrypt is
# deliberately slow (~50-100ms), so returning early only for a nonexistent
# account would let an unauthenticated caller distinguish "registered" from
# "not registered" purely from response latency, independent of the
# password guess itself. Comparing against a real hash either way keeps
# both branches paying the same cost.
DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-timing-parity-only")


def _create_token(
    subject: str, token_type: TokenType, expires_delta: timedelta, session_id: str | None = None
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": subject,
        "type": token_type.value,
        "iat": now,
        "exp": now + expires_delta,
        "jti": str(uuid.uuid4()),
    }
    if session_id is not None:
        payload["sid"] = session_id
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: uuid.UUID) -> str:
    return _create_token(
        str(user_id), TokenType.ACCESS, timedelta(minutes=settings.access_token_expire_minutes)
    )


def create_refresh_token(user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    return _create_token(
        str(user_id),
        TokenType.REFRESH,
        timedelta(days=settings.refresh_token_expire_days),
        session_id=str(session_id),
    )


class InvalidTokenError(Exception):
    pass


def decode_token_payload(token: str, expected_type: TokenType) -> dict:
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise InvalidTokenError(str(exc)) from exc

    if payload.get("type") != expected_type.value:
        raise InvalidTokenError("unexpected token type")
    if not payload.get("sub"):
        raise InvalidTokenError("missing subject")
    return payload


def decode_token(token: str, expected_type: TokenType) -> str:
    """Returns the subject (user id) if valid, raises InvalidTokenError otherwise."""
    return decode_token_payload(token, expected_type)["sub"]


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
