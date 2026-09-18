import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.auth.security import MAX_PASSWORD_BYTES


class RegisterRequest(BaseModel):
    email: EmailStr
    # `max_length` counts CHARACTERS; `hash_password` counts BYTES. Both
    # are here on purpose and neither is redundant.
    #
    # The character bound is a necessary condition -- no character encodes
    # to less than one byte -- so it rejects obvious oversize cheaply
    # before anything touches the payload. The byte check below is the
    # exact one, and it is the one that matters: bcrypt silently ignores
    # everything past 72 *bytes*, so `hash_password` refuses rather than
    # truncates (two different long passwords must never hash the same).
    #
    # They used to disagree, and the gap was not theoretical. Twenty emoji
    # are 20 characters and 80 bytes; thirty CJK characters are 90 bytes;
    # forty accented Latin letters are 80. Each passed `max_length=72`,
    # reached `hash_password`, and raised `PasswordTooLongError` -- which
    # nothing in `app/` caught. Measured through the real endpoint:
    # **HTTP 500 with a traceback** on `POST /auth/register`, unauthenticated,
    # for anyone whose password is not pure ASCII.
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_BYTES)
    name: str = Field(min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def password_fits_in_bcrypts_window(cls, value: str) -> str:
        """Reject in the same unit the security boundary uses.

        A 422 naming bytes is the honest answer: the client asked for
        something the hash cannot represent, and telling them "72
        characters" when the limit is 72 bytes would send them round the
        loop again with another password that also fails.
        """
        encoded = len(value.encode("utf-8"))
        if encoded > MAX_PASSWORD_BYTES:
            raise ValueError(
                f"Password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded "
                f"(this one is {encoded}); note that accented, CJK and emoji characters "
                "take more than one byte each"
            )
        return value


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    trading_permissions: list[str]

    model_config = {"from_attributes": True}


class SessionResponse(BaseModel):
    id: uuid.UUID
    device_info: str | None
    created_at: datetime
    expires_at: datetime

    model_config = {"from_attributes": True}
