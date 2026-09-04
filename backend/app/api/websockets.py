"""WebSocket channels (blueprint §64).

Browsers can't attach an `Authorization` header to a WebSocket handshake,
so each endpoint takes the access token as a `?token=` query parameter
instead. Every channel is a thin Redis pub/sub relay — see
`app.core.redis.publish`/`subscribe` — so multiple API replicas fan out
consistently instead of only broadcasting to clients connected to the same
process.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

from app.auth.security import InvalidTokenError, TokenType, decode_token
from app.core.redis import channel_name, subscribe
from app.database.models.users import User, UserStatus
from app.database.session import async_session_factory
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


async def _relay(websocket: WebSocket, channel: str) -> None:
    await websocket.accept()
    try:
        async for message in subscribe(channel):
            if websocket.application_state != WebSocketState.CONNECTED:
                break
            await websocket.send_json(message)
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
    await _authenticated_relay(websocket, channel_name("replay", session_id))
