import pytest
from fastapi.testclient import TestClient

from app.core.redis import get_redis, heartbeat
from app.main import app

pytestmark = pytest.mark.asyncio

# Same prefix app.core.redis.heartbeat/worker_is_alive use internally —
# duplicated here only so this test can reset state a previous run (or a
# real worker process sharing this Redis instance) may have left behind,
# since these are fixed, well-known worker names, not per-test-unique ones.
_HEARTBEAT_PREFIX = "heartbeat:worker:"
_WORKER_NAMES = ("market_data", "scanner", "auto_trade")


async def test_health_reports_worker_liveness(require_infra):
    client_redis = get_redis()
    await client_redis.delete(*(f"{_HEARTBEAT_PREFIX}{name}" for name in _WORKER_NAMES))

    with TestClient(app) as client:
        r = client.get("/health")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body.keys()) >= {"api", "database", "redis", "ai", "dhan", "upstox", "workers"}
        assert body["database"] == "HEALTHY"
        assert body["redis"] == "HEALTHY"
        # No worker process is running in this test suite, so every
        # worker should honestly read DOWN rather than a false HEALTHY.
        assert set(body["workers"].values()) == {"DOWN"}

        await heartbeat("scanner")
        r = client.get("/health")
        assert r.json()["workers"]["scanner"] == "HEALTHY"
