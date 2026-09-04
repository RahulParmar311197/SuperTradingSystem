"""Redis-backed rate limiting dependency for sensitive endpoints (login,
register) — basic brute-force / abuse mitigation."""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from app.core.config import get_settings
from app.core.redis import check_rate_limit


def rate_limit(limit: int, window_seconds: int, key_prefix: str):
    """Returns a FastAPI dependency limiting calls per client IP."""

    async def _dependency(request: Request) -> None:
        if not get_settings().rate_limit_enabled:
            return
        client_ip = request.client.host if request.client else "unknown"
        allowed = await check_rate_limit(f"{key_prefix}:{client_ip}", limit, window_seconds)
        if not allowed:
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"Too many requests — try again in under {window_seconds} seconds",
            )

    return _dependency
