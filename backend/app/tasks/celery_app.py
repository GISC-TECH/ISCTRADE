from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "trading",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.tasks.bot_tasks", "app.tasks.alert_tasks"],
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    task_soft_time_limit=120,
    task_time_limit=180,
    broker_connection_retry_on_startup=True,
    beat_schedule={
        "tick-bots": {"task": "bots.tick_all", "schedule": float(settings.BOT_TICK_SECONDS)},
        "reconcile-accounts": {"task": "accounts.reconcile_all", "schedule": float(settings.RECONCILE_SECONDS)},
        "equity-snapshots": {"task": "accounts.snapshot_all", "schedule": float(settings.EQUITY_SNAPSHOT_SECONDS)},
        "daily-risk-reset": {"task": "risk.daily_reset", "schedule": crontab(hour=0, minute=0)},
        "alerts-daily-summary": {
            "task": "alerts.daily_summary",
            "schedule": crontab(hour=settings.ALERT_DAILY_SUMMARY_HOUR_UTC, minute=5),
        },
    },
)

# Signal handlers (logging, request-id propagation, metrics). Imported for side effects.
import app.tasks.signals  # noqa: E402,F401
