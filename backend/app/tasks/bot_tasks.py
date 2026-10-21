"""Celery tasks: thin sync wrappers that run the async jobs of app.services.jobs."""

from __future__ import annotations

import uuid
from typing import Any

from app.core.config import settings
from app.services import jobs
from app.tasks.celery_app import celery_app
from app.tasks.runtime import run_async

__all__ = ["run_async"]


@celery_app.task(name="bots.tick_all")
def tick_all() -> dict[str, Any]:
    ids = run_async(jobs.due_bot_ids)
    # A tick that could not start within one period is superseded by the next one: drop it instead of
    # letting a backlog of stale ticks delay fresh ones (the per-bot Redis lock would skip them anyway).
    expires = max(float(settings.BOT_TICK_SECONDS), 5.0)
    for bot_id in ids:
        tick_bot.apply_async((str(bot_id),), expires=expires)
    return {"dispatched": len(ids)}


@celery_app.task(name="bots.tick", soft_time_limit=90, time_limit=120)
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
