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
