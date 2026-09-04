"""Root fixtures. `require_infra` gates any test that needs a real
Postgres/Redis reachable at DATABASE_URL/REDIS_URL — those tests are
skipped (not failed) when infra isn't available, so the rest of the suite
stays runnable anywhere.
"""

from __future__ import annotations

import os

# Must be set before app.core.config.get_settings() is first called
# (it's lru_cache'd) — every request in this suite shares one client "IP",
# so a real rate limit would trip on test volume, not actual abuse.
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")

import pytest  # noqa: E402

from app.core.redis import get_redis  # noqa: E402
from app.database.session import get_engine  # noqa: E402


async def _postgres_available() -> bool:
    try:
        async with get_engine().connect():
            return True
    except Exception:
        return False


async def _redis_available() -> bool:
    try:
        return bool(await get_redis().ping())
    except Exception:
        return False


@pytest.fixture
async def require_infra():
    if not await _postgres_available():
        pytest.skip("Postgres is not reachable at DATABASE_URL")
    if not await _redis_available():
        pytest.skip("Redis is not reachable at REDIS_URL")
