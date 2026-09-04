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
_WORKER_NAMES = ("market_data", "scanner", "auto_trade", "reconciliation")


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
        # market_data/scanner/auto_trade only ever heartbeat from the
        # separate `worker` process (see app/workers/main.py) — never
        # started here — so with none running they should honestly read
        # DOWN rather than a false HEALTHY.
        for name in ("market_data", "scanner", "auto_trade"):
            assert body["workers"][name] == "DOWN"
        # `reconciliation` is different: it runs inside this API process's
        # own lifespan (app.trading.live_reconciliation) and heartbeats on
        # its first pass, which has already happened by the time this
        # request lands — proof the wiring actually runs, not just that
        # the mechanism exists.
        assert body["workers"]["reconciliation"] == "HEALTHY"

        await heartbeat("scanner")
        r = client.get("/health")
        assert r.json()["workers"]["scanner"] == "HEALTHY"
