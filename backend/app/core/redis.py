"""Redis integration (blueprint §65): latest-price cache, pub/sub fanout
for WebSocket channels, and simple rate limiting.

A single client is reused per-process (`get_redis()`); it's lazy so
importing this module never requires Redis to be reachable — only calling
these functions does.
"""

from __future__ import annotations

import asyncio
import json
import weakref
from collections.abc import AsyncIterator
from datetime import datetime, timezone

import redis.asyncio as aioredis

from app.core.config import get_settings

_LATEST_PRICE_PREFIX = "price:"
_PRICE_TTL_SECONDS = 60

# redis-py's async client pins its connections to the event loop it was
# created on. A plain process-wide singleton (e.g. functools.lru_cache)
# breaks the moment anything runs a second loop — every test function
# under pytest-asyncio, or a REPL/script calling asyncio.run() more than
# once. Cache one client per *running* loop instead, keyed weakly so an
# entry drops out on its own once that loop is garbage collected. In real
# deployment (uvicorn, one loop for the process) this is exactly one
# client, same as a plain singleton would have been.
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, aioredis.Redis]" = weakref.WeakKeyDictionary()


def get_redis() -> aioredis.Redis:
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        settings = get_settings()
        client = aioredis.from_url(settings.redis_url, decode_responses=True)
        _clients[loop] = client
    return client


async def ping() -> bool:
    try:
        return await get_redis().ping()
    except Exception:
        return False


# --- Latest price cache -----------------------------------------------

_PRICE_TS_PREFIX = "price_ts:"


async def set_latest_price(symbol: str, price: float) -> None:
    now = datetime.now(timezone.utc).timestamp()
    client = get_redis()
    async with client.pipeline(transaction=True) as pipe:
        pipe.set(f"{_LATEST_PRICE_PREFIX}{symbol}", price, ex=_PRICE_TTL_SECONDS)
        pipe.set(f"{_PRICE_TS_PREFIX}{symbol}", now, ex=_PRICE_TTL_SECONDS)
        await pipe.execute()


async def get_latest_price(symbol: str) -> float | None:
    value = await get_redis().get(f"{_LATEST_PRICE_PREFIX}{symbol}")
    return float(value) if value is not None else None


async def get_price_age_seconds(symbol: str) -> float | None:
    """Seconds since the last price update for `symbol`, or None if we've
    never seen one (or the TTL has expired — same thing from a staleness
    check's point of view: there is nothing fresh to trust)."""
    value = await get_redis().get(f"{_PRICE_TS_PREFIX}{symbol}")
    if value is None:
        return None
    return max(datetime.now(timezone.utc).timestamp() - float(value), 0.0)


# --- Pub/sub fanout (backs the WebSocket channels in app.api.websockets) --

def channel_name(*parts: str) -> str:
    return ":".join(("channel", *parts))


async def publish(channel: str, payload: dict) -> None:
    await get_redis().publish(channel, json.dumps(payload, default=str))


async def subscribe(channel: str) -> AsyncIterator[dict]:
    pubsub = get_redis().pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                yield json.loads(message["data"])
            except (TypeError, ValueError):
                continue
    finally:
        await pubsub.unsubscribe(channel)
        await pubsub.aclose()


# --- Trading halts (blueprint §73-75) ------------------------------------
#
# A halt raised here must be visible to every process that can place an
# order — the API process handling the request and a background worker
# that just found a reconciliation mismatch are not the same process, so
# an in-memory flag (like KillSwitchState) can't carry this signal between
# them. Redis is the shared surface both sides already depend on.

_HALT_PREFIX = "halt:account:"


async def halt_account(account_id: str, reason: str) -> None:
    await get_redis().set(f"{_HALT_PREFIX}{account_id}", reason)


async def resume_account(account_id: str) -> None:
    await get_redis().delete(f"{_HALT_PREFIX}{account_id}")


async def account_halt_reason(account_id: str) -> str | None:
    return await get_redis().get(f"{_HALT_PREFIX}{account_id}")


# --- Rate limiting -------------------------------------------------------

async def check_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Fixed-window limiter. Returns True if the call is allowed."""
    redis_key = f"ratelimit:{key}"
    client = get_redis()
    count = await client.incr(redis_key)
    if count == 1:
        await client.expire(redis_key, window_seconds)
    return count <= limit
