"""WebSocket channels (blueprint §64).

Browsers can't attach an `Authorization` header to a WebSocket handshake,
so each endpoint takes the access token as a `?token=` query parameter
instead. Every channel is a thin Redis pub/sub relay — see
`app.core.redis.publish`/`subscribe` — so multiple API replicas fan out
consistently instead of only broadcasting to clients connected to the same
process.
"""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.auth.security import InvalidTokenError, TokenType, decode_token
from app.core.redis import channel_name, subscribe
from app.database.models.users import User, UserStatus
from app.database.session import async_session_factory
from app.replay.persistence import get_owned_replay_session
from app.users.service import get_user_by_id

router = APIRouter(tags=["websocket"])


async def _authenticate(websocket: WebSocket) -> User | None:
    token = websocket.query_params.get("token")
    if not token:
        return None
    try:
        user_id = decode_token(token, TokenType.ACCESS)
    except InvalidTokenError:
        return None

    async with async_session_factory() as db:
        user = await get_user_by_id(db, uuid.UUID(user_id))
    if user is None or user.status != UserStatus.ACTIVE:
        return None
    return user


async def _forward(websocket: WebSocket, channel: str) -> None:
    async for message in subscribe(channel):
        await websocket.send_json(message)


async def _watch_for_disconnect(websocket: WebSocket) -> None:
    # These channels are one-way broadcasts -- a client isn't expected to
    # send anything, so any message that isn't a disconnect is just
    # ignored. This is the only way to actually learn a client went away:
    # the ASGI websocket protocol only delivers a disconnect (clean close
    # *or* an abrupt drop -- a killed tab, a phone going to sleep, wifi
    # loss) as a `websocket.disconnect` message on the *receive* side.
    # `WebSocket.send()` only turns into `WebSocketDisconnect` after the
    # *next* failed write, and `_forward` above can go arbitrarily long
    # between publishes (a per-user channel like /ws/orders only publishes
    # when that user places an order) -- without a concurrent receive loop,
    # a client that disconnects between publishes left its server-side
    # task and Redis pub/sub subscription running forever, since nothing
    # else ever touches this connection (uvicorn's `connection_lost` closes
    # the transport but never cancels the running ASGI application task).
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


async def _relay(websocket: WebSocket, channel: str) -> None:
    await websocket.accept()
    forward_task = asyncio.ensure_future(_forward(websocket, channel))
    watch_task = asyncio.ensure_future(_watch_for_disconnect(websocket))
    try:
        done, pending = await asyncio.wait({forward_task, watch_task}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in pending:
            try:
                await task
            except (asyncio.CancelledError, WebSocketDisconnect):
                pass
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    except WebSocketDisconnect:
        pass
    finally:
        if websocket.application_state == WebSocketState.CONNECTED:
            await websocket.close()


async def _authenticated_relay(websocket: WebSocket, channel: str) -> None:
    user = await _authenticate(websocket)
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await _relay(websocket, channel)


@router.websocket("/ws/market")
async def ws_market(websocket: WebSocket, symbol: str) -> None:
    await _authenticated_relay(websocket, channel_name("market", symbol))


@router.websocket("/ws/chart")
async def ws_chart(websocket: WebSocket, instrument_id: str, timeframe: str) -> None:
    await _authenticated_relay(websocket, channel_name("chart", instrument_id, timeframe))


@router.websocket("/ws/scanner")
async def ws_scanner(websocket: WebSocket) -> None:
    await _authenticated_relay(websocket, channel_name("scanner"))


@router.websocket("/ws/signals")
async def ws_signals(websocket: WebSocket, instrument_id: str | None = None) -> None:
    channel = channel_name("signals", instrument_id) if instrument_id else channel_name("signals")
    await _authenticated_relay(websocket, channel)


@router.websocket("/ws/orders")
async def ws_orders(websocket: WebSocket) -> None:
    user = await _authenticate(websocket)
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await _relay(websocket, channel_name("orders", str(user.id)))


@router.websocket("/ws/positions")
async def ws_positions(websocket: WebSocket) -> None:
    user = await _authenticate(websocket)
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    await _relay(websocket, channel_name("positions", str(user.id)))


@router.websocket("/ws/replay")
async def ws_replay(websocket: WebSocket, session_id: str) -> None:
    user = await _authenticate(websocket)
    if user is None:
        await websocket.close(code=4401, reason="Unauthorized")
        return
    try:
        session_uuid = uuid.UUID(session_id)
    except ValueError:
        await websocket.close(code=4404, reason="Replay session not found")
        return
    # A replay session is private state (balance, trades, P&L) -- unlike
    # market/chart/scanner/signals, which are shared market data with
    # nothing to own, this needs the same ownership check the REST
    # /replay/* endpoints enforce, or any authenticated user could watch
    # another user's session just by knowing its UUID.
    async with async_session_factory() as db:
        owned = await get_owned_replay_session(db, session_uuid, user.id)
    if owned is None:
        await websocket.close(code=4404, reason="Replay session not found")
        return
    await _relay(websocket, channel_name("replay", session_id))
