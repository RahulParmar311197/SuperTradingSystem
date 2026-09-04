from fastapi import FastAPI
from sqlalchemy import text

from app.api.markets import router as markets_router
from app.database.session import engine

app = FastAPI(title="AI Trading Platform", version="0.1.0")
app.include_router(markets_router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    """Reports degraded rather than failing hard when a dependency is down.

    This endpoint must never claim the system is ready to trade — it only
    reports infrastructure connectivity. No execution path exists yet in
    this stage of the platform.
    """

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        database = "unavailable"

    status = "ok" if database == "ok" else "degraded"
    return {"status": status, "database": database}
