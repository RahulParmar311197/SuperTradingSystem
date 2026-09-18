"""`POST /auth/register` 500'd for anyone whose password was not ASCII.

`RegisterRequest.password` carried `Field(max_length=72)` and Pydantic
counts **characters**. `hash_password` counts **bytes** -- bcrypt silently
ignores everything past 72 of them, so it refuses rather than truncates,
because two different long passwords must never hash the same way. Nothing
in `app/` caught the `PasswordTooLongError` that refusal raises.

The two units disagreed, and the gap is ordinary rather than exotic:

    20 emoji            = 20 characters,  80 bytes
    30 CJK characters   = 30 characters,  90 bytes
    40 accented Latin   = 40 characters,  80 bytes

Each passed the schema and reached the hash. Measured through the real
endpoint before the fix: **HTTP 500 with a traceback**, on an
unauthenticated route, for a perfectly reasonable password.
"""

import uuid

import bcrypt
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import delete

from app.auth.schemas import RegisterRequest
from app.auth.security import MAX_PASSWORD_BYTES, hash_password
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

pytestmark = pytest.mark.asyncio

_EMOJI = "\U0001F600"      # 4 bytes
_CJK = "中"            # 3 bytes
_ACCENTED = "é"       # 2 bytes


def _register_payload(password: str) -> dict:
    return {
        "email": f"pwbytes{uuid.uuid4().hex[:8]}@example.com",
        "password": password,
        "name": "Password Bytes",
    }


# --- the schema now speaks the same unit as the hash ----------------------


@pytest.mark.parametrize(
    ("label", "password"),
    [("emoji", _EMOJI * 20), ("cjk", _CJK * 30), ("accented latin", _ACCENTED * 40)],
)
async def test_a_multibyte_password_inside_the_character_limit_is_rejected(label, password):
    """Behavioural proof. Each of these is well under 72 *characters* and
    over 72 *bytes* -- the exact shape that used to reach `hash_password`
    and raise."""
    assert len(password) <= MAX_PASSWORD_BYTES, "fixture must be inside the character bound"
    assert len(password.encode("utf-8")) > MAX_PASSWORD_BYTES, "fixture must exceed the byte bound"

    with pytest.raises(ValidationError) as exc:
        RegisterRequest(**_register_payload(password))

    message = str(exc.value)
    assert "bytes" in message, f"the rejection must name the real unit: {message}"
    assert str(len(password.encode("utf-8"))) in message, "and how far over it is"


async def test_the_rejection_explains_why_a_short_password_can_be_too_long():
    """Behavioural proof. "72 characters" would send a user round the loop
    with another password that also fails; the message has to say bytes and
    name the kinds of character that cost more than one."""
    with pytest.raises(ValidationError) as exc:
        RegisterRequest(**_register_payload(_EMOJI * 20))

    message = str(exc.value).lower()
    assert "emoji" in message or "cjk" in message or "accented" in message, message


async def test_an_ascii_password_at_exactly_the_limit_still_registers():
    """Control. The bound must not have quietly become stricter for the
    ordinary case -- 72 ASCII characters are 72 bytes and must pass."""
    password = "a" * MAX_PASSWORD_BYTES
    assert len(password.encode("utf-8")) == MAX_PASSWORD_BYTES
    assert RegisterRequest(**_register_payload(password)).password == password


async def test_a_short_multibyte_password_is_perfectly_fine():
    """Control, and the one that stops this becoming "no non-ASCII
    passwords". Ten emoji are 40 bytes; nothing about multi-byte characters
    is disallowed, only exceeding the window bcrypt can represent."""
    password = _EMOJI * 10
    assert len(password.encode("utf-8")) == 40
    assert RegisterRequest(**_register_payload(password)).password == password


def _bcrypt_ignores_byte(n: int) -> bool:
    """Whether bcrypt silently ignores the n-th byte (1-indexed) of a
    password -- the property `MAX_PASSWORD_BYTES` exists to fence off.

    Measured against the bcrypt library itself, on purpose. Our own
    `verify_password` cannot be the instrument: it returns False for
    anything over `MAX_PASSWORD_BYTES` before bcrypt ever sees the input,
    so asking it where bcrypt stops reading only ever answers where *we*
    stop reading. The first version of this control did exactly that and
    failed against bcrypt 4.2.0, which truncates happily.
    """
    shorter = b"a" * (n - 1)
    longer = shorter + b"b"
    hashed = bcrypt.hashpw(shorter, bcrypt.gensalt())
    try:
        return bcrypt.checkpw(longer, hashed)
    except ValueError:
        # bcrypt 5.x refuses over-long input rather than truncating it.
        # Refusing is not ignoring -- no two passwords can collide that
        # way -- but the readable window still ends before this byte, which
        # is what the caller of this helper is asking about.
        return True


async def test_the_limit_matches_where_bcrypt_actually_stops_reading():
    """Control, and the second attempt at one -- the first was vacuous.

    It asserted that the schema and `hash_password` agreed, using
    `MAX_PASSWORD_BYTES` on both sides. Injection proved it could not fail:
    moving the constant moved both sides together and the suite stayed
    green. A control that compares a value to itself tests nothing.

    The invariant is external to this codebase. Too low a limit rejects
    passwords bcrypt handles fine; too high and two different passwords
    hash identically, which is the security property the refusal exists to
    protect. Both halves are therefore measured against bcrypt, and there
    is deliberately no `assert MAX_PASSWORD_BYTES == 72` here -- that would
    pin the number without ever checking it was the right one.
    """
    from app.auth.security import PasswordTooLongError, verify_password

    assert not _bcrypt_ignores_byte(MAX_PASSWORD_BYTES), (
        f"bcrypt still reads byte {MAX_PASSWORD_BYTES}, so a limit there is not too low"
    )
    assert _bcrypt_ignores_byte(MAX_PASSWORD_BYTES + 1), (
        f"bcrypt must not read byte {MAX_PASSWORD_BYTES + 1}; if it does, the limit "
        "is above bcrypt's window and two different passwords can hash the same"
    )

    # And the codebase's own half: a password at the limit really works,
    # and one byte past it is refused rather than quietly truncated.
    at_limit = "a" * MAX_PASSWORD_BYTES
    assert verify_password(at_limit, hash_password(at_limit))
    with pytest.raises(PasswordTooLongError):
        hash_password("a" * (MAX_PASSWORD_BYTES + 1))


# --- and through the real endpoint ----------------------------------------


async def _cleanup_by_email(email: str) -> None:
    async with async_session_factory() as db:
        user = (await db.execute(User.__table__.select().where(User.email == email))).first()
        if user is not None:
            await db.execute(delete(AuditLog).where(AuditLog.user_id == user.id))
            await db.execute(delete(UserSession).where(UserSession.user_id == user.id))
            await db.execute(delete(User).where(User.id == user.id))
            await db.commit()


async def test_the_endpoint_returns_422_not_500_for_an_emoji_password(require_infra):
    """Behavioural proof at the call site.

    The schema tests above would pass just as happily if the route were
    wired to a different request model, and three consecutive earlier
    rounds found exactly that class of gap -- the logic covered, the wiring
    not. This drives the real unauthenticated route and asserts the status
    code an operator would actually see.
    """
    payload = _register_payload(_EMOJI * 20)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post("/auth/register", json=payload)

    assert r.status_code == 422, r.text
    assert "bytes" in r.text
    assert "Traceback" not in r.text, "a 500 traceback must never reach an unauthenticated caller"


async def test_the_route_answers_422_even_when_the_schema_is_bypassed(require_infra):
    """Behavioural proof for the second layer, and it took an injection to
    find out it needed one.

    Removing the route's `except PasswordTooLongError` left the whole auth
    suite green: the schema validator now rejects these passwords first, so
    the handler's catch is never reached down the ordinary path. That is
    defence in depth by definition -- and an untested defence is a claim,
    not a defence.

    `model_construct` is how a bypass actually happens: it builds the model
    without running validators, which is what any internal caller
    constructing a `RegisterRequest` directly would get. `hash_password` is
    documented as *the* security boundary, so the route has to answer 422
    rather than let the raise become the 500 this whole change is about.
    """
    from fastapi import HTTPException

    from app.api.auth import register

    payload = RegisterRequest.model_construct(
        email=f"pwbypass{uuid.uuid4().hex[:8]}@example.com",
        password=_EMOJI * 20,
        name="Password Bytes",
    )
    assert len(payload.password.encode("utf-8")) > MAX_PASSWORD_BYTES

    async with async_session_factory() as db:
        with pytest.raises(HTTPException) as exc:
            await register(payload, db)

    assert exc.value.status_code == 422, exc.value.detail
    assert "bytes" in str(exc.value.detail)


async def test_the_endpoint_still_registers_an_ordinary_password(require_infra):
    """Control for the test above. Same route, same shape, a password that
    fits -- without this, the proof would pass if register rejected
    everything."""
    payload = _register_payload("testpass123")
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/auth/register", json=payload)
        assert r.status_code == 201, r.text
    finally:
        await _cleanup_by_email(payload["email"])
