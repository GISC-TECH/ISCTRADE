"""Fire-and-forget dispatch of Celery tasks from the API (never blocks or fails a request)."""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


def enqueue(task_name: str, *args: Any, countdown: int | None = None) -> bool:
    if not settings.CELERY_DISPATCH_ENABLED:
        return False
    try:
        from app.tasks.celery_app import celery_app

        celery_app.send_task(task_name, args=[str(a) for a in args], countdown=countdown)
        return True
    except Exception as exc:  # broker down: the beat schedule will pick the work up
        logger.warning("could not enqueue %s: %s", task_name, exc)
        return False
