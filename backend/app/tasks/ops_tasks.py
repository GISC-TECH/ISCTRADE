"""Operational tasks: worker heartbeats, dead man's switch ping and data retention."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.services import retention
from app.services.heartbeats import touch_sync, worker_key
from app.tasks.celery_app import celery_app
from app.tasks.runtime import run_async

logger = logging.getLogger(__name__)


@celery_app.task(name="ops.heartbeat", bind=True, ignore_result=True, soft_time_limit=10, time_limit=20)
def heartbeat(self, queue: str | None = None) -> dict[str, Any]:
    """Scheduled by beat on each queue: proves beat -> broker -> worker of that queue end to end."""
    info = getattr(self.request, "delivery_info", None) or {}
    q = queue or info.get("routing_key") or "default"
    ok = touch_sync(worker_key(q), settings.WORKER_HEARTBEAT_STALE_SECONDS * 4)
    return {"queue": q, "written": ok}


def deadman_url(status: str) -> str:
    base = settings.DEADMAN_PING_URL.rstrip("/")
    return base if status == "ready" else f"{base}/fail"


async def _readiness(_sessions) -> dict[str, Any]:
    from app.services.health import readiness

    return await readiness()


@celery_app.task(name="ops.deadman_ping", ignore_result=True, soft_time_limit=30, time_limit=45)
def deadman_ping() -> dict[str, Any]:
    """Dead man's switch (healthchecks.io & co.): silence = beat/worker/host down; "/fail" = degraded."""
    if not settings.DEADMAN_PING_URL:
        return {"skipped": "DEADMAN_PING_URL not set"}
    result = run_async(_readiness)
    status = result.get("status", "not_ready")
    failing = sorted(k for k, v in result.get("checks", {}).items() if not v.get("ok"))
    url = deadman_url(status)
    try:
        # body (status + failing checks) shows up in the healthchecks.io event log; no secrets
        resp = httpx.post(url, content=f"{status} {','.join(failing)}".strip(), timeout=10)
        return {"status": status, "http": resp.status_code}
    except httpx.HTTPError as exc:
        logger.warning("dead man ping failed: %s", type(exc).__name__)
        return {"status": status, "error": type(exc).__name__}


@celery_app.task(name="ops.cleanup", soft_time_limit=300, time_limit=360)
def cleanup() -> dict[str, int]:
    return run_async(retention.cleanup)
