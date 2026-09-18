"""The rate limiter could poison its own key and deny an address forever.

`check_rate_limit` was `INCR`, then — only when the count came back 1 —
`EXPIRE`. Two round trips with an `await` between them. Anything that
interrupted that gap left the key with **no expiry at all**, and because
`EXPIRE` is only reached at count 1, no later call ever set one.

Measured against a live Redis by doing the `INCR` without the `EXPIRE`,
which is exactly what a crash, a cancellation, or a dropped connection on
that second call leaves behind:

    ttl after the interrupted call:        -1        (no expiry)
    next six calls (limit 3, window 1s):   True True False False False False
    after the window has passed:           False
    ttl:                                   -1

False forever. The keys are `auth:login:<client ip>` and
`auth:register:<client ip>`, so that is one address permanently unable to
log in or sign up, with no other login path, nothing that expires to
recover it, and only a human deleting the key by hand as a remedy. Redis
now has persistence (round 109, docker-compose.yml), so the poisoned key
survives a restart too.

The gap is not exotic. `app/core/rate_limit.py` already catches a Redis
error here and answers 503 — and that is precisely the interleaving that
poisons the key. The blip looks transient and leaves a permanent 429
behind it.

The fix is one atomic Lua step that also *repairs* a key which has lost
its expiry, so anything already poisoned in a running deployment heals on
its next request. It is deliberately not an unconditional `EXPIRE`:
refreshing the window on every call would mean a key under sustained load
never expires, which is the same permanent denial wearing a different hat.
That second failure mode is what the control tests here exist for.

Why nothing caught this: `tests/conftest.py` sets `RATE_LIMIT_ENABLED=false`
for the whole suite, and for a good reason — every test shares one client
address, so a real limit would trip on test volume rather than on abuse.
The effect is that no test had ever driven the limiter through an endpoint
at all. The two call-site tests at the bottom turn it back on for their own
duration and put the shared key back afterwards.
"""

import asyncio
import uuid

from fastapi.testclient import TestClient
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.redis import check_rate_limit, get_redis
from app.database.models.risk import AuditLog
from app.database.models.users import User, UserSession
from app.database.session import async_session_factory
from app.main import app


def _key() -> str:
    return f"test-ratelimit-{uuid.uuid4().hex[:10]}"


# --- the finding ---------------------------------------------------------


async def test_a_key_that_lost_its_expiry_is_repaired(require_infra):
    """Behavioural proof. This is the whole bug: before the fix the TTL
    stayed -1 forever and every later call was denied."""
    key = _key()
    redis_key = f"ratelimit:{key}"
    client = get_redis()
    try:
        # The interrupted call: the INCR landed, the EXPIRE did not.
        await client.incr(redis_key)
        assert await client.ttl(redis_key) == -1, "the fixture must start from a key with no expiry"

        assert await check_rate_limit(key, limit=3, window_seconds=30) is True
        assert await client.ttl(redis_key) > 0, "the next call must give the key an expiry"
    finally:
        await client.delete(redis_key)


async def test_a_poisoned_key_stops_denying_once_the_window_passes(require_infra):
    """Behavioural proof of the consequence, not just the mechanism. A
    repaired TTL that still denied would satisfy the test above while
    leaving the address locked out."""
    key = _key()
    redis_key = f"ratelimit:{key}"
    client = get_redis()
    try:
        # Poison it well past the limit, the way a real burst would.
        for _ in range(5):
            await client.incr(redis_key)
        assert await client.ttl(redis_key) == -1

        assert await check_rate_limit(key, limit=3, window_seconds=1) is False, "still over the limit, correctly"
        await asyncio.sleep(1.3)
        assert await check_rate_limit(key, limit=3, window_seconds=1) is True, "the window must actually expire"
    finally:
        await client.delete(redis_key)


async def test_the_first_call_sets_an_expiry(require_infra):
    """Behavioural proof that the ordinary path cannot leave a key
    unexpiring — the state the bug depended on is no longer reachable
    through this function at all."""
    key = _key()
    redis_key = f"ratelimit:{key}"
    client = get_redis()
    try:
        assert await check_rate_limit(key, limit=3, window_seconds=45) is True
        ttl = await client.ttl(redis_key)
        assert 0 < ttl <= 45, ttl
    finally:
        await client.delete(redis_key)


# --- and what the fix must not break -------------------------------------


async def test_the_window_is_not_extended_by_later_calls(require_infra):
    """Control, and the one that rules out the tempting wrong fix. Calling
    `EXPIRE` on every request would repair the TTL too — and would mean a
    key under sustained traffic never expires, which is the same permanent
    denial this round is removing."""
    key = _key()
    redis_key = f"ratelimit:{key}"
    client = get_redis()
    try:
        await check_rate_limit(key, limit=100, window_seconds=3)
        first = await client.ttl(redis_key)
        await asyncio.sleep(1.2)
        await check_rate_limit(key, limit=100, window_seconds=3)
        second = await client.ttl(redis_key)
        assert second < first, f"the window must keep counting down: {first} -> {second}"
    finally:
        await client.delete(redis_key)


async def test_the_limit_still_bites(require_infra):
    """Control. A limiter that repaired every TTL and allowed everything
    would pass every proof above."""
    key = _key()
    try:
        results = [await check_rate_limit(key, limit=3, window_seconds=30) for _ in range(5)]
        assert results == [True, True, True, False, False], results
    finally:
        await get_redis().delete(f"ratelimit:{key}")


async def test_two_keys_do_not_share_a_counter(require_infra):
    """Control. The limiter is per client address; one address exhausting
    its budget must not deny another."""
    busy, quiet = _key(), _key()
    try:
        for _ in range(4):
            await check_rate_limit(busy, limit=3, window_seconds=30)
        assert await check_rate_limit(busy, limit=3, window_seconds=30) is False
        assert await check_rate_limit(quiet, limit=3, window_seconds=30) is True
    finally:
        await get_redis().delete(f"ratelimit:{busy}", f"ratelimit:{quiet}")


# --- where the harm actually lands ---------------------------------------


async def test_login_recovers_from_a_poisoned_limiter_key(require_infra, monkeypatch):
    """Behavioural proof at the call site.

    `POST /auth/login` is the endpoint with no alternative: an address
    locked out here cannot reach the API at all. The key is poisoned the
    way an interrupted call leaves it, and the assertion is that the
    endpoint's own use of the limiter puts an expiry back — without it,
    every login from this address is 429 until someone deletes the key in
    Redis by hand.
    """
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    client = get_redis()
    email = f"ratelimit-{uuid.uuid4().hex[:8]}@example.com"
    # TestClient's peer address, which is what `client_ip` resolves to with
    # trusted_proxy_hops at its default of 0.
    redis_key = "ratelimit:auth:login:testclient"
    try:
        await client.delete(redis_key)
        for _ in range(11):  # the login limit is 10/60s
            await client.incr(redis_key)
        assert await client.ttl(redis_key) == -1

        with TestClient(app) as http:
            r = http.post("/auth/login", json={"email": email, "password": "testpass123"})
            assert r.status_code == 429, r.text
        assert await client.ttl(redis_key) > 0, (
            "the refused request must still have repaired the expiry, or this address "
            "is locked out of login permanently"
        )
    finally:
        # Shared with every other test that touches /auth — always leave it
        # clean rather than merely expiring.
        await client.delete(redis_key)


async def test_an_ordinary_login_is_not_rate_limited(require_infra, monkeypatch):
    """Control. With the limiter genuinely on, real traffic must still get
    through — a version that denied everything would satisfy the proof
    above, since that one also asserts a 429."""
    monkeypatch.setattr(get_settings(), "rate_limit_enabled", True)
    client = get_redis()
    login_key = "ratelimit:auth:login:testclient"
    register_key = "ratelimit:auth:register:testclient"
    email = f"ratelimit-ok-{uuid.uuid4().hex[:8]}@example.com"
    user_id = None
    try:
        await client.delete(login_key, register_key)
        with TestClient(app) as http:
            r = http.post("/auth/register", json={"email": email, "password": "testpass123", "name": "RL"})
            assert r.status_code == 201, r.text
            r = http.post("/auth/login", json={"email": email, "password": "testpass123"})
            assert r.status_code == 200, r.text

            from app.auth.security import TokenType, decode_token

            user_id = uuid.UUID(decode_token(r.json()["access_token"], TokenType.ACCESS))
    finally:
        await client.delete(login_key, register_key)
        if user_id is not None:
            async with async_session_factory() as db:
                await db.execute(delete(AuditLog).where(AuditLog.user_id == user_id))
                await db.execute(delete(UserSession).where(UserSession.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
                await db.commit()
