from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    ai,
    auth,
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
)
from app.core.config import get_settings
from app.monitoring.health import router as health_router

settings = get_settings()

app = FastAPI(title=settings.app_name, debug=settings.debug)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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


@app.get("/")
async def root() -> dict:
    return {"name": settings.app_name, "status": "ok"}
