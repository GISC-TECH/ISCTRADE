"""Async runtime for Celery tasks: one persistent event loop per worker process.

Previously every task ran ``asyncio.run`` with a NullPool engine and closed Redis afterwards, so each bot tick
paid a new event loop, a new PostgreSQL connection (TCP + SCRAM auth), a new Redis connection and a new
TCP/TLS handshake to Binance. Keeping one loop per (prefork) process lets the DB pool, the Redis client and
the pooled Binance HTTP client (app.services.binance_http) be reused across tasks.

Safety:
* The loop is created lazily in the child process (pid-checked), never inherited across ``fork``.
* Tasks left behind by a job are cancelled after it returns, like ``asyncio.run`` does.
* If a job is interrupted from outside the coroutine (Celery soft time limit, KeyboardInterrupt) the whole
  runtime is discarded and rebuilt on the next task.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.database import make_engine

logger = logging.getLogger(__name__)
T = TypeVar("T")


@dataclass
class _Runtime:
    pid: int
    loop: asyncio.AbstractEventLoop
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


_rt: _Runtime | None = None


def _make_task_engine() -> AsyncEngine:
    size = int(settings.CELERY_DB_POOL_SIZE)
    if size <= 0:
        return make_engine(nullpool=True)
    return make_engine(nullpool=False, pool_size=size, max_overflow=max(size, 2))


def _runtime() -> _Runtime:
    global _rt
    if _rt is None or _rt.pid != os.getpid() or _rt.loop.is_closed():
        loop = asyncio.new_event_loop()
        engine = _make_task_engine()
        _rt = _Runtime(
            os.getpid(), loop, engine, async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        )
    return _rt


async def _close_resources(engine: AsyncEngine) -> None:
    from app.core.redis import close_redis
    from app.services.binance_http import close_shared_clients

    for closer in (close_shared_clients(), close_redis(), engine.dispose()):
        try:
            await asyncio.wait_for(closer, timeout=5)
        except BaseException:  # pragma: no cover - best effort
            pass


def _cancel_leftovers(loop: asyncio.AbstractEventLoop) -> None:
    pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
    if not pending:
        return
    for t in pending:
        t.cancel()
    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


def shutdown_runtime() -> None:
    """Close pools and the loop (worker shutdown, or after an interrupted job)."""
    global _rt
    rt, _rt = _rt, None
    if rt is None or rt.pid != os.getpid() or rt.loop.is_closed():
        return
    try:
        _cancel_leftovers(rt.loop)
        rt.loop.run_until_complete(_close_resources(rt.engine))
        rt.loop.run_until_complete(rt.loop.shutdown_asyncgens())
    except BaseException:  # pragma: no cover - a broken loop is simply dropped
        logger.debug("async runtime shutdown failed", exc_info=True)
    finally:
        try:
            rt.loop.close()
        except BaseException:  # pragma: no cover
            pass


def run_async(fn: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]) -> T:
    """Run `fn(sessions)` on the worker's persistent loop and return its result."""
    rt = _runtime()
    task = rt.loop.create_task(fn(rt.sessions))
    try:
        result = rt.loop.run_until_complete(task)
    except BaseException:
        if not task.done():  # interrupted from outside the coroutine: loop state is unknown
            task.cancel()
            shutdown_runtime()
            raise
        _cancel_leftovers(rt.loop)
        raise
    _cancel_leftovers(rt.loop)
    return result
