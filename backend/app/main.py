import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import (
    ai,
    auth,
    auto_trading,
    backtest,
    brokers,
    charts,
    markets,
    options,
    orders,
    paper,
    portfolio,
    positions,
    replay,
    scanner,
    strategies,
    websockets,
)
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.metrics import metrics_response
from app.core.middleware import RequestContextMiddleware
from app.monitoring.health import check_database, check_redis
from app.monitoring.health import router as health_router

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
    yield
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
app.include_router(websockets.router)


@app.get("/metrics")
async def metrics():
    return metrics_response()


@app.get("/")
async def root() -> dict:
    return {"name": settings.app_name, "status": "ok"}
