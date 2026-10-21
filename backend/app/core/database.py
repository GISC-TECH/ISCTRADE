"""Async SQLAlchemy 2.0 engine and session factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import settings


def make_engine(
    url: str | None = None,
    *,
    nullpool: bool | None = None,
    pool_size: int | None = None,
    max_overflow: int | None = None,
) -> AsyncEngine:
    url = url or settings.DATABASE_URL
    use_nullpool = settings.DB_USE_NULLPOOL if nullpool is None else nullpool
    kwargs: dict = {"echo": settings.DB_ECHO}
    if use_nullpool or url.startswith("sqlite"):
        # NullPool: connections are never shared between event loops (Celery tasks, tests).
        kwargs["poolclass"] = NullPool
    else:
        kwargs.update(
            pool_pre_ping=True,
            pool_size=settings.DB_POOL_SIZE if pool_size is None else pool_size,
            max_overflow=settings.DB_MAX_OVERFLOW if max_overflow is None else max_overflow,
            pool_recycle=1800,  # drop connections idle-killed by PgBouncer/firewalls before they error out
        )
    return create_async_engine(url, **kwargs)


engine = make_engine()
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Standalone session for background jobs (commit is explicit)."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
