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


@pytest.fixture(autouse=True)
async def _dispose_infra_clients_after_test():
    """`get_engine()`/`get_redis()` cache one client per *running* event
    loop (see their module docstrings) so a real deployment gets exactly
    one client for the process's lifetime — but pytest-asyncio gives each
    test function its own loop, and dropping a loop from that cache when
    it's garbage collected does NOT close the connections its engine/client
    were holding: SQLAlchemy's pool and redis-py's connections stay open
    server-side until a TCP timeout eventually reaps them. Across a full
    suite run that leaks a live Postgres connection per test — verified
    firsthand: `pg_stat_activity` climbed from ~9 to 97 (of a 100 default
    `max_connections`) over one run, silently *skipping* (not failing) any
    `require_infra` test unlucky enough to run after the limit was hit.
    Disposing both here, in the same loop the test just used and before
    pytest-asyncio tears that loop down, closes the connections cleanly
    instead of leaving them for Postgres/Redis to notice on their own.

    This only covers connections opened on *this test's own* event loop
    (e.g. its own `async_session_factory()` calls for setup/cleanup) --
    it does nothing for connections opened inside the app during a
    `with TestClient(app):` block, since that runs the whole app on its
    own separate, freshly-created event loop every time. See
    `app/main.py`'s lifespan shutdown for the fix to that half."""
    yield
    try:
        await get_engine().dispose()
    except Exception:
        pass
    try:
        await get_redis().aclose()
    except Exception:
        pass
