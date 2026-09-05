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
_PRICE_PREV_PREFIX = "price_prev:"


async def set_latest_price(symbol: str, price: float) -> None:
    now = datetime.now(timezone.utc).timestamp()
    client = get_redis()
    # Stash whatever was latest a moment ago as "previous" before
    # overwriting it, so `get_price_jump_pct` has a tick to diff the new
    # one against (blueprint §57 "unexpected price jump"). A plain read
    # before the pipeline, not part of its transaction: `market_data_worker`
    # is the only writer, one tick at a time on one loop, so there's no
    # concurrent writer this could race with.
    previous = await get_latest_price(symbol)
    async with client.pipeline(transaction=True) as pipe:
        if previous is not None:
            pipe.set(f"{_PRICE_PREV_PREFIX}{symbol}", previous, ex=_PRICE_TTL_SECONDS)
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


async def get_price_jump_pct(symbol: str) -> float | None:
    """Percent change between the current latest price and the tick
    immediately before it, or None if there's no previous tick to compare
    against yet (same "nothing to trust" convention as
    `get_price_age_seconds` — a brand-new symbol or one whose keys expired
    isn't a price jump, it's just no data)."""
    client = get_redis()
    latest = await get_latest_price(symbol)
    previous_raw = await client.get(f"{_PRICE_PREV_PREFIX}{symbol}")
    if latest is None or previous_raw is None:
        return None
    previous = float(previous_raw)
    if previous == 0:
        return None
    return abs(latest - previous) / previous * 100


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


# --- OAuth state (CSRF protection for the Upstox/Dhan authorize flow) ----

_OAUTH_STATE_PREFIX = "oauth_state:"
_OAUTH_STATE_TTL_SECONDS = 600


async def store_oauth_state(state: str, user_id: str) -> None:
    await get_redis().set(f"{_OAUTH_STATE_PREFIX}{state}", user_id, ex=_OAUTH_STATE_TTL_SECONDS)


async def pop_oauth_state(state: str) -> str | None:
    """Returns the user id that initiated this OAuth flow, and consumes
    the state token so it can't be replayed."""
    client = get_redis()
    async with client.pipeline(transaction=True) as pipe:
        pipe.get(f"{_OAUTH_STATE_PREFIX}{state}")
        pipe.delete(f"{_OAUTH_STATE_PREFIX}{state}")
        value, _ = await pipe.execute()
    return value


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


async def list_halted_accounts() -> dict[str, str]:
    """Every account currently halted, keyed by account id. Blueprint
    §75 says resuming is a deliberate manual step, not automatic — that
    step needs somewhere to see *what's* halted and why first (see
    `GET /admin/halted-accounts`), rather than only discovering a halt by
    hitting it with an order and getting a 423."""
    client = get_redis()
    halted: dict[str, str] = {}
    async for key in client.scan_iter(match=f"{_HALT_PREFIX}*"):
        reason = await client.get(key)
        if reason is not None:
            halted[key[len(_HALT_PREFIX):]] = reason
    return halted


# --- Worker heartbeats (blueprint §117 "Workers 🟢") ----------------------
#
# The worker process (see app/workers/main.py) is separate from the API
# process serving GET /health, so "is the scanner loop actually iterating"
# has to be answered through shared state, same as the trading halt above.
# A short TTL key that each worker's loop refreshes on every pass means a
# stuck or crashed loop goes stale within one interval — no separate
# liveness check to maintain.

_HEARTBEAT_PREFIX = "heartbeat:worker:"
_HEARTBEAT_TTL_SECONDS = 30


async def heartbeat(worker_name: str) -> None:
    await get_redis().set(f"{_HEARTBEAT_PREFIX}{worker_name}", "1", ex=_HEARTBEAT_TTL_SECONDS)


async def worker_is_alive(worker_name: str) -> bool:
    return await get_redis().exists(f"{_HEARTBEAT_PREFIX}{worker_name}") > 0


# --- Rate limiting -------------------------------------------------------

async def check_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Fixed-window limiter. Returns True if the call is allowed."""
    redis_key = f"ratelimit:{key}"
    client = get_redis()
    count = await client.incr(redis_key)
    if count == 1:
        await client.expire(redis_key, window_seconds)
    return count <= limit
