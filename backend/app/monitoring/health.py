"""System health checks (blueprint §72, §117)."""

from __future__ import annotations

import logging
from enum import StrEnum

from fastapi import APIRouter
from sqlalchemy import text

from app.core.redis import ping as redis_ping
from app.core.redis import worker_is_alive
from app.database.session import get_engine

logger = logging.getLogger("monitoring.health")

router = APIRouter(tags=["monitoring"])


class ComponentStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    NOT_CONFIGURED = "NOT_CONFIGURED"


# The worker names each loop actually heartbeats under — see
# app/workers/main.py, app/workers/scanner_worker.py,
# app/workers/auto_trade_worker.py, and app/trading/live_reconciliation.py
# (the last one runs inside this API process, not the separate `worker`
# process, but is still worth reporting here for the same reason).
_WORKER_NAMES = ("market_data", "scanner", "auto_trade", "reconciliation")


async def check_database() -> ComponentStatus:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return ComponentStatus.HEALTHY
    except Exception:
        return ComponentStatus.DOWN


async def check_redis() -> ComponentStatus:
    return ComponentStatus.HEALTHY if await redis_ping() else ComponentStatus.DOWN


async def check_workers() -> dict[str, str]:
    """Blueprint §117 "Workers 🟢": each name is DOWN until its loop in the
    separate `worker` process (see app/workers/main.py) has heartbeated at
    least once within the last 30s — this can legitimately read DOWN in
    an environment where the worker process was never started, which is
    the honest answer, not a false HEALTHY.

    Guarded, because `worker_is_alive` is a bare `redis.exists`
    (app/core/redis.py) and heartbeats live in Redis. Unguarded, a Redis
    outage raised out of here and `GET /health` answered 500 -- measured,
    while `check_database` and `check_redis` both degraded politely to
    DOWN beside it. That is the worst possible moment for this endpoint to
    die: it is what a load balancer polls, what an uptime monitor pages
    on, and the first thing an operator opens during an outage, and the
    `redis: DOWN` line it would have shown is the whole explanation.

    DOWN is the honest answer here rather than a new "unknown" state. The
    contract this function already has is "no heartbeat observed in the
    last 30s", and an unreachable heartbeat store is exactly that: no
    evidence of life. Reporting HEALTHY would be a claim nothing supports.
    """
    try:
        return {
            name: (ComponentStatus.HEALTHY if await worker_is_alive(name) else ComponentStatus.DOWN).value
            for name in _WORKER_NAMES
        }
    except Exception:
        logger.exception("Could not read worker heartbeats (Redis unreachable?); reporting every worker DOWN")
        return {name: ComponentStatus.DOWN.value for name in _WORKER_NAMES}


@router.get("/health")
async def health() -> dict:
    from app.core.config import get_settings

    settings = get_settings()

    return {
        "api": ComponentStatus.HEALTHY.value,
        "database": (await check_database()).value,
        "redis": (await check_redis()).value,
        "ai": (ComponentStatus.HEALTHY if settings.ai_api_key else ComponentStatus.NOT_CONFIGURED).value,
        "dhan": ComponentStatus.NOT_CONFIGURED.value,
        "upstox": ComponentStatus.NOT_CONFIGURED.value,
        "workers": await check_workers(),
    }
