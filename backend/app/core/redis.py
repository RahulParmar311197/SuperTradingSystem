"""Redis integration (blueprint §65): latest-price cache, pub/sub fanout
for WebSocket channels, and simple rate limiting.

A single client is reused per-process (`get_redis()`); it's lazy so
importing this module never requires Redis to be reachable — only calling
these functions does.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
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


# --- Kill switch (blueprint §58) ------------------------------------------
#
# Same cross-process problem as the account halts above: `KillSwitchState`
# (app.risk.kill_switch) is a plain in-memory dataclass, so a kill
# triggered from one process (an admin API call) was invisible to the
# RiskEngine instances living inside every other process's -- and even the
# same process's other -- per-user trading stacks. These keys are the
# shared surface; app.risk.kill_switch.load_kill_switch_state reads them
# back into a KillSwitchState for RiskEngine/evaluate_options_risk to
# consult, the same way `account_halt_reason` above is read by orders.py.

_KILL_GLOBAL_KEY = "kill:global"
_KILL_ACCOUNT_PREFIX = "kill:account:"
_KILL_STRATEGY_PREFIX = "kill:strategy:"


async def set_global_kill() -> None:
    await get_redis().set(_KILL_GLOBAL_KEY, "1")


async def clear_global_kill() -> None:
    await get_redis().delete(_KILL_GLOBAL_KEY)


async def is_global_killed() -> bool:
    return await get_redis().get(_KILL_GLOBAL_KEY) is not None


async def set_account_kill(account_id: str) -> None:
    await get_redis().set(f"{_KILL_ACCOUNT_PREFIX}{account_id}", "1")


async def clear_account_kill(account_id: str) -> None:
    await get_redis().delete(f"{_KILL_ACCOUNT_PREFIX}{account_id}")


async def is_account_killed(account_id: str) -> bool:
    return await get_redis().get(f"{_KILL_ACCOUNT_PREFIX}{account_id}") is not None


async def set_strategy_kill(strategy_id: str) -> None:
    await get_redis().set(f"{_KILL_STRATEGY_PREFIX}{strategy_id}", "1")


async def clear_strategy_kill(strategy_id: str) -> None:
    await get_redis().delete(f"{_KILL_STRATEGY_PREFIX}{strategy_id}")


async def is_strategy_killed(strategy_id: str) -> bool:
    return await get_redis().get(f"{_KILL_STRATEGY_PREFIX}{strategy_id}") is not None


async def list_killed_accounts() -> list[str]:
    client = get_redis()
    return [key[len(_KILL_ACCOUNT_PREFIX) :] async for key in client.scan_iter(match=f"{_KILL_ACCOUNT_PREFIX}*")]


async def list_killed_strategies() -> list[str]:
    client = get_redis()
    return [key[len(_KILL_STRATEGY_PREFIX) :] async for key in client.scan_iter(match=f"{_KILL_STRATEGY_PREFIX}*")]


# --- Worker heartbeats (blueprint §117 "Workers 🟢") ----------------------
#
# The worker process (see app/workers/main.py) is separate from the API
# process serving GET /health, so "is the scanner loop actually iterating"
# has to be answered through shared state, same as the trading halt above.
# A TTL key that each worker's loop refreshes on every pass means a stuck
# or crashed loop goes stale within one interval — no separate liveness
# check to maintain. That only holds if the TTL comfortably exceeds every
# worker's actual refresh cadence, though: scanner_worker.py,
# auto_trade_worker.py, and live_reconciliation.py all call `heartbeat()`
# once per 60-second loop (`interval_seconds=60.0`, hardcoded at every
# call site — not env-configurable, so a single constant here is safe).
# The TTL must be comfortably longer than that, with margin for one slow
# pass — not shorter, which would make `worker_is_alive()` flap
# False/True every cycle for a perfectly healthy worker (this was the bug:
# 30s < 60s meant the key expired for roughly the back half of every
# cycle).
_HEARTBEAT_PREFIX = "heartbeat:worker:"
_HEARTBEAT_TTL_SECONDS = 90


async def heartbeat(worker_name: str) -> None:
    await get_redis().set(f"{_HEARTBEAT_PREFIX}{worker_name}", "1", ex=_HEARTBEAT_TTL_SECONDS)


async def worker_is_alive(worker_name: str) -> bool:
    return await get_redis().exists(f"{_HEARTBEAT_PREFIX}{worker_name}") > 0


# --- Rate limiting -------------------------------------------------------

# INCR and the TTL have to land together, and the TTL has to be repairable.
#
# This was `incr` and then, only when the count came back 1, `expire` --
# two round trips with an await between them. Anything that interrupted
# that gap left the key with **no expiry at all**, and because `expire` is
# only reached at count 1, no later call ever set one. Measured against a
# live Redis by doing the INCR without the EXPIRE, exactly as a crash, a
# cancellation or a dropped connection on the second call leaves things:
#
#     ttl after the interrupted call: -1        (no expiry)
#     next six calls (limit 3, window 1s): True True False False False False
#     after the window has passed:          False
#     ttl:                                   -1
#
# False forever. The keys are `auth:login:<client ip>` and
# `auth:register:<client ip>`, so that is one address permanently unable
# to log in or sign up, with no other login path and nothing that expires
# to recover -- only a human deleting the key by hand. Redis now has
# persistence (see docker-compose.yml), so the poisoned key survives a
# restart too.
#
# The gap is not exotic: it is one await between two round trips, and the
# caller (app/core/rate_limit.py) already handles a Redis error there by
# answering 503. That is exactly the interleaving that poisons the key --
# the blip looks transient and leaves a permanent 429 behind it.
#
# Lua, because it must be one atomic step. The TTL check also *repairs* a
# key that has somehow lost its expiry, rather than only setting one on
# the first call, so any key already poisoned in a running deployment
# heals on its next request. Deliberately not an unconditional EXPIRE:
# refreshing the window on every call would mean a key under sustained
# load never expires, which is the same permanent denial wearing a
# different hat.
_RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def check_rate_limit(key: str, limit: int, window_seconds: int) -> bool:
    """Fixed-window limiter. Returns True if the call is allowed."""
    redis_key = f"ratelimit:{key}"
    count = await get_redis().eval(_RATE_LIMIT_SCRIPT, 1, redis_key, window_seconds)
    return int(count) <= limit


# ---------------------------------------------------------------------------
# Per-account trade lock (cross-process)
#
# `app/api/orders.py::serialize_user_trading` is an `asyncio.Lock` held in the
# API process. docker-compose.yml runs `api` and `worker` as SEPARATE
# services, so `AutoTradeSupervisor` is always a different process and that
# lock cannot span them. Measured on current main, with the worker held
# provably mid-fill (past its risk gate, position not yet in Postgres):
#
#     race    Postgres exposure 0.00      POST /orders -> 201
#             final: manual 80,000 + auto 31,212 = 111.2% of a 100,000 account
#     control Postgres exposure 31,212.12 SAME order   -> 403 "Projected
#             exposure 111.21%"
#
# Same order, same sizes; only the timing differs. The gate names the very
# figure it should have refused -- it simply could not see the other
# process's exposure yet. `max_exposure_pct` is 100.
#
# Redis rather than anything new: it is already this system's cross-process
# coordination layer (the kill switch, account halts, and the atomic Lua
# rate limiter above), and docker-compose runs it with `--appendonly yes`.
_TRADE_LOCK_PREFIX = "tradelock:account:"

# BOTH of these must stay inside `RiskLimits.market_data_max_staleness_seconds`
# (10.0). That coupling is not obvious and it bit during development: with a
# 15s TTL and a 20s wait, a queued order waited ~15s and was then refused
# with `Data age 15.03` -- the lock had manufactured the very staleness that
# rejected the trade, and the user saw a confusing freshness error after a
# long hang instead of either a fill or a clean "account busy".
#
# Long enough to cover a broker round trip plus the persist that follows,
# short enough that a process killed mid-trade does not wedge an account for
# long. A holder that outlives this loses the lock rather than holding it
# forever: the same "acquired and never released" failure the in-process lock
# needed its own test for.
TRADE_LOCK_TTL_SECONDS = 8

# How long a caller waits for a busy account before giving up. The point is
# to queue behind the other process rather than refuse, so this covers a
# full hold -- but no more, for the reason above.
TRADE_LOCK_WAIT_SECONDS = 8

_LOCK_POLL_SECONDS = 0.05

# Release only if we still hold it. Without the value check, a holder whose
# TTL had already expired would delete the NEXT holder's lock on its way
# out, handing the account to two processes at once -- the bug this exists
# to prevent, wearing a different hat.
_TRADE_LOCK_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""


async def acquire_trade_lock(account_id: str, *, wait_seconds: float | None = None) -> str | None:
    """Take this account's cross-process trade lock.

    Returns an opaque token to pass to `release_trade_lock`, or None if the
    account stayed busy for `wait_seconds`. Redis errors propagate: the
    caller decides fail-open vs fail-closed, the way
    `app/core/rate_limit.py` already does for the limiter.
    """
    token = uuid.uuid4().hex
    key = f"{_TRADE_LOCK_PREFIX}{account_id}"
    deadline = time.monotonic() + (TRADE_LOCK_WAIT_SECONDS if wait_seconds is None else wait_seconds)
    while True:
        # NX+PX in one call: two processes cannot both see it free.
        if await get_redis().set(key, token, nx=True, px=TRADE_LOCK_TTL_SECONDS * 1000):
            return token
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_LOCK_POLL_SECONDS)


async def release_trade_lock(account_id: str, token: str) -> None:
    """Release a lock taken with `acquire_trade_lock`. A no-op if the TTL
    already expired and someone else holds it now."""
    key = f"{_TRADE_LOCK_PREFIX}{account_id}"
    await get_redis().eval(_TRADE_LOCK_RELEASE_SCRIPT, 1, key, token)
