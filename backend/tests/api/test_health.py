import time

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
        # its first pass — but `asyncio.create_task` only *schedules* that
        # coroutine at startup, it doesn't guarantee any of its body has
        # run by the time this first request lands (that's a real race,
        # confirmed by CI: reliably HEALTHY on a fast local run, still
        # DOWN on a slower scheduler). Poll briefly instead of assuming
        # same-tick completion — this still proves the wiring actually
        # runs, just without a flaky timing assumption.
        for _ in range(50):
            if client.get("/health").json()["workers"]["reconciliation"] == "HEALTHY":
                break
            time.sleep(0.05)
        else:
            pytest.fail("live reconciliation loop never heartbeated within ~2.5s of API startup")

        await heartbeat("scanner")
        r = client.get("/health")
        assert r.json()["workers"]["scanner"] == "HEALTHY"


# --- /health must survive the outage it exists to report -----------------


class _DeadRedis:
    """Every call raises the way redis-py does when nothing is listening."""

    async def exists(self, *args, **kwargs):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")

    async def ping(self, *args, **kwargs):
        raise ConnectionError("Error 111 connecting to redis:6379. Connection refused.")


async def test_check_workers_survives_an_unreachable_redis(monkeypatch):
    """The bug. `worker_is_alive` is a bare `redis.exists`, so with Redis
    down this raised while its two siblings degraded politely:

        check_database   -> DOWN
        check_redis      -> DOWN
        check_workers    -> RAISED ConnectionError

    and `GET /health` answered 500.
    """
    import app.core.redis as core_redis
    from app.monitoring.health import check_workers

    monkeypatch.setattr(core_redis, "get_redis", lambda: _DeadRedis())

    workers = await check_workers()

    assert workers, "must still report every worker, not an empty dict"
    assert set(workers.values()) == {"DOWN"}


async def test_health_answers_200_and_names_redis_as_the_cause(monkeypatch):
    # The consequence that matters. This endpoint is what a load balancer
    # polls and what an operator opens first during an outage; a 500 here
    # withholds the `redis: DOWN` line that explains everything.
    import app.core.redis as core_redis
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.monitoring.health import router

    monkeypatch.setattr(core_redis, "get_redis", lambda: _DeadRedis())

    api = FastAPI()
    api.include_router(router)
    with TestClient(api, raise_server_exceptions=False) as client:
        response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["redis"] == "DOWN"
    assert set(body["workers"].values()) == {"DOWN"}
    # The API itself is still answering, which is the true thing to say.
    assert body["api"] == "HEALTHY"


async def test_a_reachable_redis_still_distinguishes_live_workers_from_dead_ones(require_infra):
    # Control: the guard must not flatten everything to DOWN. With Redis
    # up, a worker that has heartbeated reads HEALTHY and one that has not
    # reads DOWN — otherwise this traded a 500 for a permanently useless
    # answer.
    from app.core.redis import heartbeat
    from app.monitoring.health import _WORKER_NAMES, check_workers

    await heartbeat(_WORKER_NAMES[0])
    workers = await check_workers()

    assert workers[_WORKER_NAMES[0]] == "HEALTHY"
    assert set(workers.values()) != {"HEALTHY"}, "every worker reading HEALTHY would make this vacuous"
