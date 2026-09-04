"""System health checks (blueprint §72, §117)."""

from __future__ import annotations

from enum import StrEnum

from fastapi import APIRouter
from sqlalchemy import text

from app.core.redis import ping as redis_ping
from app.database.session import get_engine

router = APIRouter(tags=["monitoring"])


class ComponentStatus(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    NOT_CONFIGURED = "NOT_CONFIGURED"


async def check_database() -> ComponentStatus:
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return ComponentStatus.HEALTHY
    except Exception:
        return ComponentStatus.DOWN


async def check_redis() -> ComponentStatus:
    return ComponentStatus.HEALTHY if await redis_ping() else ComponentStatus.DOWN


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
    }
