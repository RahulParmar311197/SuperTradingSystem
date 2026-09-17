"""Redis-backed rate limiting dependency for sensitive endpoints (login,
register) — basic brute-force / abuse mitigation."""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.core.redis import check_rate_limit

logger = logging.getLogger("core.rate_limit")

# What a denied-because-unavailable response tells the client to wait.
# Short: a Redis blip is usually seconds, and this is the login page.
_UNAVAILABLE_RETRY_AFTER_SECONDS = 5


def client_ip(request: Request, trusted_proxy_hops: int) -> str:
    """The address to rate-limit this request under.

    `request.client.host` is the socket peer. With nothing in front of the
    process that is the real client, but behind a reverse proxy it is the
    *proxy* — the same value for everyone — so the per-IP limiter silently
    becomes one global bucket. Measured against the repo's own
    `infrastructure/nginx/nginx.conf.example`: twelve requests from twelve
    distinct client addresses through one proxy, and the eleventh and
    twelfth were rejected 429 under a limit of 10/minute. On the login
    endpoint that is a denial of service anyone can trigger, deliberately
    or just by being the eleventh person to sign in that minute.

    `X-Forwarded-For` is only consulted when `trusted_proxy_hops` says a
    proxy is actually there, and never as the whole story. nginx's
    `$proxy_add_x_forwarded_for` *appends* the peer it saw, so the chain
    reads `<whatever the client sent>, <addresses each proxy observed>`.
    Everything a client can forge sits on the left; counting back from the
    right by the number of proxies you run lands on the address the
    innermost trusted proxy actually observed. Reading the leftmost entry
    instead — the common shortcut — would let any client set its own
    address, evade the limiter entirely, and lock a chosen victim out of
    login by claiming to be them.

    Defaulting to 0 keeps a deployment with no proxy correct: a forged
    header is ignored outright rather than trusted by a process that has
    no way to know whether anything sanitised it.
    """
    peer = request.client.host if request.client else "unknown"
    if trusted_proxy_hops <= 0:
        return peer
    chain = [part.strip() for part in request.headers.get("x-forwarded-for", "").split(",")]
    chain = [part for part in chain if part]
    if len(chain) < trusted_proxy_hops:
        # Fewer entries than proxies configured: this request did not
        # traverse the chain the operator described, so nothing in the
        # header is attributable. Fall back to the peer, which is real
        # whatever happened upstream.
        return peer
    return chain[-trusted_proxy_hops]


def rate_limit(limit: int, window_seconds: int, key_prefix: str):
    """Returns a FastAPI dependency limiting calls per client IP."""

    async def _dependency(request: Request) -> None:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return
        try:
            allowed = await check_rate_limit(
                f"{key_prefix}:{client_ip(request, settings.trusted_proxy_hops)}",
                limit,
                window_seconds,
            )
        except Exception:
            # `check_rate_limit` is a bare `redis.incr`/`expire`
            # (app/core/redis.py) and nothing used to catch this, so a
            # Redis outage raised straight through the dependency and
            # `POST /auth/login` answered 500 -- measured. That is the
            # wrong answer whichever way the policy goes: it claims this
            # service has a defect when the truth is that a dependency is
            # unreachable, and `app/core/middleware.py` counts it into
            # `http_requests_total{status_code="500"}`, so a Redis blip
            # shows up in the metric an operator watches for real bugs.
            logger.exception(
                "Rate limiter could not reach Redis for %s; %s",
                key_prefix,
                "allowing the request (rate_limit_fail_open=True)"
                if settings.rate_limit_fail_open
                else "refusing the request (rate_limit_fail_open=False)",
            )
            if settings.rate_limit_fail_open:
                return
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Rate limiting is temporarily unavailable — try again shortly",
                headers={"Retry-After": str(_UNAVAILABLE_RETRY_AFTER_SECONDS)},
            ) from None
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Too many requests — try again in under {window_seconds} seconds",
            )

    return _dependency
