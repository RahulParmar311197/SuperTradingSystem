import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    admin,
    ai,
    auth,
    auto_trading,
    backtest,
    brokers,
    charts,
    markets,
    notifications,
    options,
    orders,
    paper,
    portfolio,
    positions,
    replay,
    scanner,
    strategies,
    trading_permissions,
    websockets,
)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import metrics_response
from app.core.middleware import RequestContextMiddleware
from app.core.redis import get_redis
from app.database.session import get_engine
from app.monitoring.health import check_database, check_redis
from app.monitoring.health import router as health_router
from app.trading.live_reconciliation import run as run_live_reconciliation

settings = get_settings()
configure_logging("DEBUG" if settings.debug else "INFO")
logger = logging.getLogger("app")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db_status = await check_database()
    redis_status = await check_redis()
    logger.info("Startup checks: database=%s redis=%s", db_status.value, redis_status.value)
    if db_status.value != "HEALTHY":
        logger.warning("Database is not reachable at startup — API will serve requests but most will fail")
    if redis_status.value != "HEALTHY":
        logger.warning("Redis is not reachable at startup — caching, pub/sub and rate limiting will fail")

    # Runs in this process, not the separate `worker` process — see
    # app.trading.live_reconciliation's module docstring for why.
    reconciliation_task = asyncio.create_task(run_live_reconciliation(), name="live_reconciliation")
    yield
    reconciliation_task.cancel()
    try:
        await reconciliation_task
    except asyncio.CancelledError:
        pass

    # `get_engine()`/`get_redis()` cache one client per *running* event
    # loop (see their module docstrings). That loop is normally this
    # process's only one and lives for the process's whole lifetime -- but
    # every `with TestClient(app):` block runs this entire lifespan on its
    # own fresh internal event loop (a new one on every use, even within
    # a single outer process), and neither client was ever disposed here.
    # Concretely: a Postgres connection opened by `check_database()` above,
    # by the reconciliation task, or by any request this instance served,
    # stayed open server-side for as long as it took Postgres to notice
    # and reap it -- verified firsthand, repeatedly instantiating
    # `TestClient(app)` in a single Python process (one outer event loop
    # the whole time) leaked one new cached engine, and two new live
    # Postgres connections, every single time, something no per-test
    # fixture disposing its own loop's engine could ever catch since the
    # app's connections were never on that loop to begin with. Disposing
    # both here — the same loop this instance's connections actually used
    # — is what a graceful shutdown should always do anyway, not just a
    # test-suite accommodation.
    try:
        await get_engine().dispose()
    except Exception:
        pass
    try:
        await get_redis().aclose()
    except Exception:
        pass
    logger.info("Shutting down")


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)

app.add_middleware(RequestContextMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    detail = str(exc) if settings.debug else "Internal server error"
    return JSONResponse(status_code=500, content={"detail": detail})


app.include_router(health_router)
app.include_router(auth.router)
app.include_router(markets.router)
app.include_router(charts.router)
app.include_router(scanner.router)
app.include_router(ai.router)
app.include_router(strategies.router)
app.include_router(options.router)
app.include_router(replay.router)
app.include_router(backtest.router)
app.include_router(paper.router)
app.include_router(orders.router)
app.include_router(positions.router)
app.include_router(portfolio.router)
app.include_router(brokers.router)
app.include_router(auto_trading.router)
app.include_router(trading_permissions.router)
app.include_router(admin.router)
app.include_router(notifications.router)
app.include_router(websockets.router)


@app.get("/metrics")
async def metrics():
    return metrics_response()


@app.get("/")
async def root() -> dict:
    return {"name": settings.app_name, "status": "ok"}
