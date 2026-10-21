"""News ingestion and economic calendar tasks."""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.services.news import calendar, store
from app.tasks.bot_tasks import run_async
from app.tasks.celery_app import celery_app


@celery_app.task(name="news.ingest", soft_time_limit=240, time_limit=270)
def ingest_news() -> dict[str, Any]:
    if not settings.NEWS_ENABLED:
        return {"skipped": "disabled"}
    return run_async(store.ingest)


@celery_app.task(name="calendar.refresh", soft_time_limit=60, time_limit=90)
def refresh_calendar() -> dict[str, Any]:
    return run_async(calendar.refresh_calendar)


async def _alert_events(sessions) -> dict[str, Any]:
    async with sessions() as db:
        n = await store.alert_upcoming_events(db)
        await db.commit()
    return {"alerts": n}


@celery_app.task(name="calendar.alerts", soft_time_limit=30, time_limit=45)
def alert_upcoming_events() -> dict[str, Any]:
    return run_async(_alert_events)
