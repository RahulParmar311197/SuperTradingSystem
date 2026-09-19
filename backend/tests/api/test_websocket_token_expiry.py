"""An open WebSocket outlived the access token that opened it.

Round 110 made session *revocation* reach open streams, because the
handshake check alone left revocation applying to REST and not to sockets.
The token's own expiry was left in exactly that state: `_authenticate`
verifies `exp` once, and nothing checked it again, so a stream ran for as
long as the client cared to stay connected on a thirty-minute credential.

Measured against the real endpoint with a three-second token:

    while valid:  socket received the published order event
    after expiry: GET /auth/sessions with the same token -> 401
    after expiry: the SOCKET still delivered that user's order event

That gap matters more than the usual "authenticate at the handshake"
shortcut suggests, because the token arrives as a `?token=` query
parameter -- the one place credentials routinely end up in proxy and
server logs. The thirty-minute expiry is the bound on what a leaked token
is worth, and an unbounded stream of a user's live orders and positions
removes it.

The fix is a deadline task in `_relay`, beside the revocation watcher: a
poll is the right shape for revocation, which can happen at any moment,
and the wrong shape for an instant already known at the handshake.
"""

import asyncio
import json
import queue
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from starlette.websockets import WebSocketDisconnect

from app.api.websockets import _watch_for_token_expiry
from app.auth.security import TokenType, _create_token, decode_token_payload
from app.core.redis import channel_name, publish
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app

# Long enough to open a socket, publish through it and check REST on the
# way past; short enough that the whole file stays a few seconds.
SHORT_LIFETIME = 3.0



# Every read in this file goes through here. `WebSocketTestSession.receive`
# blocks on an unbounded `queue.get()`, so a socket that *stays open* when
# it should have closed makes `pytest.raises(WebSocketDisconnect)` wait
# forever -- the first injection run of this file was killed after twenty
# minutes having reported nothing, which is round 152's lesson arriving by
# a different door: a control that hangs proves nothing. Mirrors starlette's
# own `receive_json` (`_send_queue` -> `_raise_on_close` -> `json.loads`),
# with a deadline on the one call that lacks it.
_DEADLINE_SECONDS = 20.0


def _receive_json(ws, what: str):
    try:
        message = ws._send_queue.get(timeout=_DEADLINE_SECONDS)
    except queue.Empty:
        raise AssertionError(f"{what}: nothing arrived within {_DEADLINE_SECONDS}s") from None
    if isinstance(message, BaseException):
        raise message
    ws._raise_on_close(message)
    return json.loads(message["text"])


def _expect_closed(ws, what: str) -> None:
    """Asserts the socket has closed, within the deadline. A socket left
    open reports that here instead of hanging."""
    try:
        delivered = _receive_json(ws, what)
    except WebSocketDisconnect:
        return
    raise AssertionError(f"{what}: the socket was still open and delivered {delivered}")


async def _account(client) -> tuple[uuid.UUID, uuid.UUID, str]:
    """Returns (user id, session id, a normal access token)."""
    email = f"wsexp-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "WS Exp"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]
    payload = decode_token_payload(token, TokenType.ACCESS)
    return uuid.UUID(payload["sub"]), uuid.UUID(payload["sid"]), token


def _token_lasting(seconds: float, user_id: uuid.UUID, session_id: uuid.UUID) -> str:
    """A real access token for a real session, just a short-lived one --
    minted the same way `create_access_token` does, with a different
    lifetime, so nothing about it is a fake except how long it lasts."""
    return _create_token(
        str(user_id), TokenType.ACCESS, timedelta(seconds=seconds), session_id=str(session_id)
    )


async def _delete_user(user_id: uuid.UUID) -> None:
    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


# --- the finding ----------------------------------------------------------


async def test_a_socket_closes_when_its_access_token_expires(require_infra):
    """Behavioural proof through the real endpoint and the real Redis
    channel `POST /orders` publishes on. The REST check in the middle is
    the contrast that makes the point: the same token, at the same moment,
    refused by every request route and still feeding the stream."""
    with TestClient(app) as client:
        user_id, session_id, _ = await _account(client)
        short = _token_lasting(SHORT_LIFETIME, user_id, session_id)
        try:
            with client.websocket_connect(f"/ws/orders?token={short}") as ws:
                await publish(channel_name("orders", str(user_id)), {"event": "before"})
                assert _receive_json(ws, "the first order event") == {"event": "before"}, "fixture: the stream must work first"

                await asyncio.sleep(SHORT_LIFETIME + 1.0)

                rest = client.get("/auth/sessions", headers={"Authorization": f"Bearer {short}"})
                assert rest.status_code == 401, "fixture: REST must already refuse the expired token"

                await publish(channel_name("orders", str(user_id)), {"event": "after"})
                _expect_closed(ws, "the order socket after its token expired")
        finally:
            await _delete_user(user_id)


async def test_every_authenticated_channel_gets_the_same_deadline(require_infra):
    """`/ws/market` reaches `_relay` through `_authenticated_relay`, the
    shared path the market, chart, scanner and signals channels all take.
    A fix wired only into the two per-user channels would leave those four
    running on an expired token."""
    with TestClient(app) as client:
        user_id, session_id, _ = await _account(client)
        short = _token_lasting(SHORT_LIFETIME, user_id, session_id)
        try:
            with client.websocket_connect(f"/ws/market?symbol=TESTSYM&token={short}") as ws:
                await publish(channel_name("market", "TESTSYM"), {"tick": 1})
                assert _receive_json(ws, "the first market tick") == {"tick": 1}, "fixture: the stream must work first"

                await asyncio.sleep(SHORT_LIFETIME + 1.0)
                await publish(channel_name("market", "TESTSYM"), {"tick": 2})
                _expect_closed(ws, "the market socket after its token expired")
        finally:
            await _delete_user(user_id)


# --- what the fix must not break -----------------------------------------


async def test_a_socket_on_an_unexpired_token_stays_open(require_infra):
    """Control, and the one that matters: a change that closed every
    socket promptly -- or read the deadline as already past -- would
    satisfy both proofs above. This socket outlives the short lifetime the
    other tests use, on an ordinary login token."""
    with TestClient(app) as client:
        user_id, _session_id, token = await _account(client)
        try:
            with client.websocket_connect(f"/ws/orders?token={token}") as ws:
                await asyncio.sleep(SHORT_LIFETIME + 1.0)
                await publish(channel_name("orders", str(user_id)), {"event": "still here"})
                assert _receive_json(ws, "the event after the short lifetime") == {"event": "still here"}
        finally:
            await _delete_user(user_id)


async def test_the_deadline_is_the_tokens_own_not_a_fixed_timeout(require_infra):
    """Two sockets for one account, opened together on tokens with
    different lifetimes. A fixed timeout, or one read off the session
    rather than the token, would end them together."""
    with TestClient(app) as client:
        user_id, session_id, _ = await _account(client)
        short = _token_lasting(SHORT_LIFETIME, user_id, session_id)
        longer = _token_lasting(SHORT_LIFETIME * 10, user_id, session_id)
        try:
            with client.websocket_connect(f"/ws/orders?token={short}") as short_ws:
                with client.websocket_connect(f"/ws/orders?token={longer}") as long_ws:
                    # A relay subscribes to Redis after the handshake
                    # returns, and pub/sub has no history: a message
                    # published before the second subscription lands
                    # reaches only the first socket. Measured -- without
                    # this settle the second socket blocked through the
                    # publish. Nothing about the fix, just what a fan-out
                    # relay is.
                    await asyncio.sleep(0.3)
                    await publish(channel_name("orders", str(user_id)), {"event": "before"})
                    assert _receive_json(short_ws, "short socket, before") == {"event": "before"}
                    assert _receive_json(long_ws, "long socket, before") == {"event": "before"}

                    await asyncio.sleep(SHORT_LIFETIME + 1.0)
                    await publish(channel_name("orders", str(user_id)), {"event": "after"})

                    assert _receive_json(long_ws, "long socket, after") == {"event": "after"}, (
                        "the longer-lived token's socket must still be open"
                    )
                    _expect_closed(short_ws, "the short-lived token's socket")
        finally:
            await _delete_user(user_id)


async def test_a_token_with_no_expiry_cannot_open_a_stream(require_infra):
    """The branch added with the deadline. `_create_token` always sets
    `exp`, so this is unreachable through any token this codebase mints --
    which is the reason to refuse rather than default it: a token that got
    here without one did not come from here, and defaulting would hand it
    a stream with no deadline at all."""
    from jose import jwt

    from app.core.config import get_settings

    settings = get_settings()
    with TestClient(app) as client:
        user_id, session_id, _ = await _account(client)
        no_exp = jwt.encode(
            {
                "sub": str(user_id),
                "type": TokenType.ACCESS.value,
                "iat": datetime.now(timezone.utc),
                "jti": str(uuid.uuid4()),
                "sid": str(session_id),
            },
            settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
        )
        try:
            # Valid signature, live session, real user -- everything but a
            # deadline.
            assert decode_token_payload(no_exp, TokenType.ACCESS).get("exp") is None
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/ws/orders?token={no_exp}") as ws:
                    _receive_json(ws, "a stream opened with no deadline")
        finally:
            await _delete_user(user_id)


# --- the watcher itself ---------------------------------------------------


async def test_the_watcher_returns_at_once_for_a_deadline_already_past():
    """Pure. A socket whose token expired between the handshake check and
    the first await must not wait out a negative sleep."""
    await asyncio.wait_for(
        _watch_for_token_expiry(datetime.now(timezone.utc) - timedelta(hours=1)), timeout=1.0
    )


async def test_the_watcher_waits_for_a_deadline_still_ahead():
    """The other half: it must not return immediately for every deadline,
    which would close every socket the moment it opened."""
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(
            _watch_for_token_expiry(datetime.now(timezone.utc) + timedelta(hours=1)), timeout=0.2
        )
