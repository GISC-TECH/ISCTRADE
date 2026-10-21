"""Market scanner task (beat: every SCANNER_REFRESH_SECONDS)."""

from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.services import scanner
from app.tasks.bot_tasks import run_async
from app.tasks.celery_app import celery_app


@celery_app.task(name="scanner.run", soft_time_limit=150, time_limit=170)
def run_scanner() -> dict[str, Any]:
    if not settings.SCANNER_ENABLED:
        return {"skipped": "disabled"}
    return run_async(scanner.run_and_store)
