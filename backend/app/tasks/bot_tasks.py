"""Celery tasks. Each task runs its async job in a fresh event loop with a NullPool engine."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.database import make_engine
from app.core.redis import close_redis
from app.services import jobs
from app.tasks.celery_app import celery_app

T = TypeVar("T")

_engine = make_engine(nullpool=True)
_sessions = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


def run_async(fn: Callable[[async_sessionmaker[AsyncSession]], Awaitable[T]]) -> T:
    async def runner() -> T:
        try:
            return await fn(_sessions)
        finally:
            await close_redis()

    return asyncio.run(runner())


@celery_app.task(name="bots.tick_all")
def tick_all() -> dict[str, Any]:
    ids = run_async(jobs.due_bot_ids)
    for bot_id in ids:
        tick_bot.delay(str(bot_id))
    return {"dispatched": len(ids)}


@celery_app.task(name="bots.tick")
def tick_bot(bot_id: str) -> dict[str, Any]:
    return run_async(lambda s: jobs.tick_bot(s, uuid.UUID(bot_id)))


@celery_app.task(name="bots.start")
def start_bot(bot_id: str) -> dict[str, Any]:
    result = run_async(lambda s: jobs.start_bot(s, uuid.UUID(bot_id)))
    if result.get("status") == "prepared":
        tick_bot.delay(bot_id)  # first tick right away (STARTING -> RUNNING)
    return result


@celery_app.task(name="accounts.reconcile")
def reconcile(account_id: str) -> dict[str, Any]:
    return run_async(lambda s: jobs.reconcile_account(s, uuid.UUID(account_id)))


@celery_app.task(name="accounts.reconcile_all")
def reconcile_all() -> dict[str, Any]:
    ids = run_async(jobs.live_account_ids)
    for account_id in ids:
        reconcile.delay(str(account_id))
    return {"dispatched": len(ids)}


@celery_app.task(name="accounts.snapshot")
def snapshot(account_id: str) -> dict[str, Any]:
    return run_async(lambda s: jobs.snapshot_account(s, uuid.UUID(account_id)))


@celery_app.task(name="accounts.snapshot_all")
def snapshot_all() -> dict[str, Any]:
    ids = run_async(jobs.snapshot_account_ids)
    for account_id in ids:
        snapshot.delay(str(account_id))
    return {"dispatched": len(ids)}


@celery_app.task(name="risk.daily_reset")
def daily_reset() -> dict[str, Any]:
    return {"accounts": run_async(jobs.daily_reset)}
