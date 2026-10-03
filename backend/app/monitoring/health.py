"""System health checks (blueprint §72, §117)."""

from __future__ import annotations

import logging
from enum import StrEnum

from fastapi import APIRouter
from sqlalchemy import select, text

from app.core.redis import ping as redis_ping
from app.core.redis import worker_is_alive
from app.database.models.users import BrokerAccount, BrokerAccountStatus, BrokerName
from app.database.session import async_session_factory, get_engine

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


# The brokers this endpoint reports on, and the response keys they use.
# `BrokerName.PAPER` is deliberately absent: it is not an external service
# whose reachability an operator can act on.
_BROKER_COMPONENTS = ((BrokerName.DHAN, "dhan"), (BrokerName.UPSTOX, "upstox"))


def _status_for(statuses: list[BrokerAccountStatus]) -> ComponentStatus:
    """What this deployment's accounts for one broker add up to.

    HEALTHY means at least one ACTIVE account, i.e. `resolve_broker` would
    hand a real adapter to somebody and their orders would go to this
    broker for real. DOWN means accounts exist but none is ACTIVE: it was
    set up and is not usable now, which is the state worth paging on.
    NOT_CONFIGURED means no account has ever been connected.
    """
    if not statuses:
        return ComponentStatus.NOT_CONFIGURED
    if BrokerAccountStatus.ACTIVE in statuses:
        return ComponentStatus.HEALTHY
    return ComponentStatus.DOWN


async def check_brokers() -> dict[str, str]:
    """Blueprint §117 "Brokers": whether orders can actually reach each one.

    Both of these used to be the literal `ComponentStatus.NOT_CONFIGURED`,
    never computed from anything. Measured against the real app:

        nothing configured          upstox NOT_CONFIGURED  ai NOT_CONFIGURED
        credentials set in settings upstox NOT_CONFIGURED  ai HEALTHY
        one ACTIVE UPSTOX account   upstox NOT_CONFIGURED
          (resolve_broker -> UpstoxBroker, so that user's orders are LIVE)

    The middle line is the clearest proof it was a constant: `ai` on the
    same response flips, and these two never did. The last line is the
    harm. `/health` is public, unauthenticated, and -- as round 125 put it
    when it stopped this endpoint 500ing -- "the one endpoint you consult
    during an outage". It reported no broker connected while real orders
    were routing to Upstox, which is the wrong direction for a monitoring
    surface to be wrong in.

    Derived from `broker_accounts` rather than from `settings`, although
    `upstox_client_id`/`upstox_secret` exist and would have been the exact
    parallel of the `ai` line above. Those settings gate only the OAuth
    routes: `POST /brokers/connect` takes credentials directly and never
    reads them, so an account can be ACTIVE -- and placing live orders --
    on a deployment where they are unset. A settings-derived answer would
    have left the measured case still reporting NOT_CONFIGURED.

    This does disclose, to an unauthenticated caller, whether the
    deployment has any connected account per broker. That is an aggregate
    deployment fact of the same kind as the `ai`, `database` and `workers`
    lines already here, it names no user and counts nothing, and it is the
    fact the endpoint exists to report.

    Guarded like `check_workers`, and for the same reason: a database
    outage must not take `GET /health` down with it. DOWN rather than
    NOT_CONFIGURED on that path because it is the true answer -- with the
    database unreachable no order can be placed through any broker -- and
    the `database: DOWN` line beside it is the explanation.
    """
    try:
        async with async_session_factory() as db:
            rows = (await db.execute(select(BrokerAccount.broker, BrokerAccount.status))).all()
    except Exception:
        logger.exception("Could not read broker accounts (database unreachable?); reporting every broker DOWN")
        return {key: ComponentStatus.DOWN.value for _, key in _BROKER_COMPONENTS}

    return {
        key: _status_for([status for broker, status in rows if broker == name]).value
        for name, key in _BROKER_COMPONENTS
    }


@router.get("/health")
async def health() -> dict:
    from app.core.config import get_settings

    settings = get_settings()

    return {
        "api": ComponentStatus.HEALTHY.value,
        "database": (await check_database()).value,
        "redis": (await check_redis()).value,
        "ai": (ComponentStatus.HEALTHY if settings.ai_api_key else ComponentStatus.NOT_CONFIGURED).value,
        **await check_brokers(),
        "workers": await check_workers(),
    }
