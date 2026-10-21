"""Alert delivery tasks (queue: default). Retries with exponential backoff; never touches trading state."""

from __future__ import annotations

from typing import Any

from app.services.alerts import service
from app.tasks.bot_tasks import run_async
from app.tasks.celery_app import celery_app

MAX_RETRIES = 5


@celery_app.task(name="alerts.process", ignore_result=True)
def process(payload: dict[str, Any]) -> dict[str, Any]:
    result = run_async(lambda s: service.process_alert(s, payload))
    if result.get("status") == "pending":
        deliver.delay(result["alert_id"])
    return result


@celery_app.task(
    bind=True,
    name="alerts.deliver",
    ignore_result=True,
    autoretry_for=(service.DeliveryError,),
    retry_backoff=10,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=MAX_RETRIES,
)
def deliver(self, alert_id: str) -> dict[str, Any]:
    final = self.request.retries >= MAX_RETRIES
    return run_async(lambda s: service.deliver_alert(s, alert_id, final=final))


@celery_app.task(name="alerts.daily_summary")
def daily_summary() -> dict[str, Any]:
    return run_async(service.daily_summary)
