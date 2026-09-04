"""System health checks (blueprint §72, §117)."""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter
from sqlalchemy import text

from app.core.redis import ping as redis_ping
from app.core.redis import worker_is_alive
from app.database.session import get_engine

router = APIRouter(tags=["monitoring"])


class ComponentStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    NOT_CONFIGURED = "NOT_CONFIGURED"


# The worker names each loop actually heartbeats under — see
# app/workers/main.py, app/workers/scanner_worker.py, and
# app/workers/auto_trade_worker.py.
_WORKER_NAMES = ("market_data", "scanner", "auto_trade")


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
    the honest answer, not a false HEALTHY."""
    return {name: (ComponentStatus.HEALTHY if await worker_is_alive(name) else ComponentStatus.DOWN).value for name in _WORKER_NAMES}


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
