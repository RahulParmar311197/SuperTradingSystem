import asyncio
import uuid

import pytest
from starlette.websockets import WebSocketState

from app.api.websockets import _relay
from app.core.redis import get_redis

pytestmark = pytest.mark.asyncio


class _FakeWebSocket:
    """Minimal `WebSocket` double whose `receive()` resolves to a client
    disconnect after a short delay, and whose `send_json()`/`accept()` are
    no-ops -- enough surface for `_relay` without a real ASGI connection.
    `starlette.testclient.WebSocketTestSession` can't stand in for this:
    its own `__exit__` cancels the underlying app task unconditionally
    (see anyio TaskGroup teardown in `_run`), which would mask this exact
    bug -- the task getting cancelled by test harness plumbing regardless
    of whether `_relay` ever learned about the disconnect itself."""

    def __init__(self) -> None:
        self.application_state = WebSocketState.CONNECTED
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self) -> None:
        pass

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)

    async def receive(self) -> dict:
        await asyncio.sleep(0.05)
        return {"type": "websocket.disconnect", "code": 1000}

    async def close(self) -> None:
        self.closed = True
        self.application_state = WebSocketState.DISCONNECTED


async def test_relay_detects_disconnect_and_cleans_up_the_subscription(require_infra):
    # Regression test: `_relay` used to only ever `await`
    # `subscribe(channel)` and `send_json` -- it never called
    # `websocket.receive()`, the only path the ASGI websocket protocol
    # uses to deliver a disconnect (clean close *or* an abrupt drop). A
    # channel with nothing new to publish (true of every real channel
    # between events -- /ws/orders only publishes when that user places an
    # order) left `_relay` parked forever, leaking its task and the
    # underlying Redis pub/sub subscription for the rest of the process's
    # life. Proven directly here: nothing is ever published to this
    # channel, so the pre-fix `_relay` would block on `subscribe(channel)`
    # forever regardless of the fake websocket's disconnect -- this test
    # would time out. The fix's concurrent receive-loop watchdog notices
    # the disconnect immediately and tears the subscription down.
    channel = f"test:relay:{uuid.uuid4().hex[:8]}"
    ws = _FakeWebSocket()

    await asyncio.wait_for(_relay(ws, channel), timeout=2.0)

    assert ws.closed is True
    numsub = dict(await get_redis().pubsub_numsub(channel))
    assert numsub[channel] == 0


# --- revoking a session must end its open streams too ----------------------
#
# Revocation is enforced on every REST request by `get_current_user`, which
# looks the session up each time. A WebSocket has no requests to hang that
# check on, and the handshake check happens once -- so an already-open
# socket kept delivering. Measured end to end before the fix, with a socket
# open on `/ws/orders`: `POST /auth/sessions/{id}/revoke` answered 204, the
# same token then got 401 from REST, and the socket still received the
# user's order events. `/ws/orders` and `/ws/positions` carry that user's
# live order and position events, which is exactly what someone revoking a
# session on a stolen or shared device is trying to cut off.


async def _register_and_login(client) -> tuple[str, uuid.UUID, uuid.UUID]:
    """Returns (access token, user id, session id)."""
    email = f"wsrevoke-{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/auth/register", json={"email": email, "password": "testpass123", "name": "WS Revoke"})
    assert r.status_code == 201, r.text
    token = client.post("/auth/login", json={"email": email, "password": "testpass123"}).json()["access_token"]

    from app.auth.security import TokenType, decode_token_payload

    payload = decode_token_payload(token, TokenType.ACCESS)
    return token, uuid.UUID(payload["sub"]), uuid.UUID(payload["sid"])


async def _delete_user(user_id: uuid.UUID) -> None:
    from sqlalchemy import delete

    from app.database.models.risk import AuditLog
    from app.database.models.users import User, UserSession
    from app.database.session import async_session_factory

    async with async_session_factory() as db:
        await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
        await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
        await db.execute(delete(User).where(User.id == user_id))
        await db.commit()


async def test_revoking_a_session_closes_its_open_order_stream(require_infra, monkeypatch):
    """Behavioural proof, through the real endpoint and the real Redis
    channel `POST /orders` publishes on.

    The re-check interval is shortened rather than waited out -- what is
    being proven is that the socket closes at all, not the production
    cadence, which `SESSION_RECHECK_SECONDS` states.
    """
    import app.api.websockets as websockets_module
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from app.core.redis import channel_name, publish
    from app.main import app

    monkeypatch.setattr(websockets_module, "SESSION_RECHECK_SECONDS", 0.05)

    with TestClient(app) as client:
        token, user_id, session_id = await _register_and_login(client)
        headers = {"Authorization": f"Bearer {token}"}
        try:
            with client.websocket_connect(f"/ws/orders?token={token}") as ws:
                await publish(channel_name("orders", str(user_id)), {"event": "before"})
                assert ws.receive_json() == {"event": "before"}, "fixture: the stream must work first"

                assert client.post(f"/auth/sessions/{session_id}/revoke", headers=headers).status_code == 204
                assert client.get("/auth/sessions", headers=headers).status_code == 401, (
                    "fixture: REST must already refuse the revoked token"
                )

                await asyncio.sleep(0.4)
                await publish(channel_name("orders", str(user_id)), {"event": "after"})
                with pytest.raises(WebSocketDisconnect):
                    ws.receive_json()
        finally:
            await _delete_user(user_id)


async def test_a_live_session_keeps_its_stream_open(require_infra, monkeypatch):
    """Control, and the one that matters: the watcher must end sockets
    whose session is gone and no others. With the interval at 50ms this
    socket outlives many re-checks, so a watcher that closed on anything
    but revocation -- an exception read as "revoked", a query that returns
    nothing for a live session -- fails here.
    """
    import app.api.websockets as websockets_module
    from fastapi.testclient import TestClient

    from app.core.redis import channel_name, publish
    from app.main import app

    monkeypatch.setattr(websockets_module, "SESSION_RECHECK_SECONDS", 0.05)

    with TestClient(app) as client:
        token, user_id, _session_id = await _register_and_login(client)
        try:
            with client.websocket_connect(f"/ws/orders?token={token}") as ws:
                await asyncio.sleep(0.4)
                await publish(channel_name("orders", str(user_id)), {"event": "still here"})
                assert ws.receive_json() == {"event": "still here"}
        finally:
            await _delete_user(user_id)


async def test_the_relay_without_a_session_still_works(require_infra):
    """Control on the parameter itself. `_relay`'s `session_id` is
    optional, so a caller that passes none must behave exactly as before
    rather than closing immediately or never watching anything."""
    channel = f"test:relay:{uuid.uuid4().hex[:8]}"
    ws = _FakeWebSocket()

    await asyncio.wait_for(_relay(ws, channel), timeout=2.0)

    assert ws.closed is True
