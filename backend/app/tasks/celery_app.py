from celery import Celery
from celery.schedules import crontab
from kombu import Exchange, Queue

from app.core.config import settings

celery_app = Celery(
    "trading",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.bot_tasks",
        "app.tasks.alert_tasks",
        "app.tasks.scanner_tasks",
        "app.tasks.news_tasks",
        "app.tasks.trading_tasks",
    ],
)
# Queues: trading-critical work (bot ticks, paper order book, reconciliation, equity/risk) never waits behind
# slow I/O-bound jobs (news + LLM, scanner, calendar, WhatsApp delivery). Run one worker per queue
# (docker-compose: celery_worker_trading -Q trading, celery_worker -Q default). A worker started without -Q
# consumes both queues, so a single-worker setup keeps working.
TRADING_QUEUE = "trading"
DEFAULT_QUEUE = "default"
TASK_ROUTES = {
    "bots.*": {"queue": TRADING_QUEUE},
    "paper.*": {"queue": TRADING_QUEUE},
    "accounts.*": {"queue": TRADING_QUEUE},
    "risk.*": {"queue": TRADING_QUEUE},
    "scanner.*": {"queue": DEFAULT_QUEUE},
    "news.*": {"queue": DEFAULT_QUEUE},
    "calendar.*": {"queue": DEFAULT_QUEUE},
    "alerts.*": {"queue": DEFAULT_QUEUE},
}

celery_app.conf.update(
    # explicit exchange/routing key per queue: with shared bindings a message would be copied to both queues
    task_queues=tuple(Queue(q, Exchange(q, type="direct"), routing_key=q) for q in (TRADING_QUEUE, DEFAULT_QUEUE)),
    task_default_queue=DEFAULT_QUEUE,
    task_routes=TASK_ROUTES,
    # Nobody reads task results: skip one Redis write (and key expiry) per task. Tasks that need a result
    # can still opt in with ignore_result=False.
    task_ignore_result=True,
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
        "market-scanner": {"task": "scanner.run", "schedule": float(settings.SCANNER_REFRESH_SECONDS)},
        "news-ingest": {"task": "news.ingest", "schedule": float(settings.NEWS_REFRESH_SECONDS)},
        "calendar-refresh": {"task": "calendar.refresh", "schedule": float(settings.ECON_CALENDAR_REFRESH_SECONDS)},
        "calendar-alerts": {"task": "calendar.alerts", "schedule": 60.0},
        "paper-orders": {"task": "paper.process_orders", "schedule": float(settings.PAPER_ORDER_CHECK_SECONDS)},
        "alerts-daily-summary": {
            "task": "alerts.daily_summary",
            "schedule": crontab(hour=settings.ALERT_DAILY_SUMMARY_HOUR_UTC, minute=5),
        },
    },
)

# Signal handlers (logging, request-id propagation, metrics). Imported for side effects.
import app.tasks.signals  # noqa: E402,F401
