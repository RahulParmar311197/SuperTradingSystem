import asyncio
import weakref
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings

settings = get_settings()


class Base(DeclarativeBase):
    pass


# SQLAlchemy's async engine pins its connection pool to the event loop it
# was created on (same underlying constraint as redis-py's async client —
# see app/core/redis.py's comment). A plain module-level singleton breaks
# across event loops: every pytest-asyncio test function gets a fresh loop,
# and a worker process that ever restarts its loop would hit this too.
# Cache one engine per *running* loop instead, weakly, so it's exactly one
# engine for the life of a real single-loop deployment (uvicorn) — the
# same as a plain singleton would have been — but safe under multiple loops.
_engines: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, AsyncEngine]" = weakref.WeakKeyDictionary()


def get_engine() -> AsyncEngine:
    loop = asyncio.get_running_loop()
    engine = _engines.get(loop)
    if engine is None:
        engine = create_async_engine(settings.database_url, echo=settings.debug, future=True)
        _engines[loop] = engine
    return engine


def async_session_factory() -> AsyncSession:
    return AsyncSession(get_engine(), expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_factory() as session:
        yield session
