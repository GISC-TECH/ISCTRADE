"""Monitoring status for the dashboard (readiness + alert channel, no secrets)."""

from __future__ import annotations

import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context
from app.core.config import settings
from app.core.database import get_db
from app.models.alert import AlertRecord
from app.schemas.alert import MonitoringStatus
from app.services.alerts.events import AlertEvent
from app.services.alerts.service import channels_status, emit_alert
from app.services.alerts.whatsapp import channel_status
from app.services.health import readiness

router = APIRouter()


@router.get("/status", response_model=MonitoringStatus)
async def monitoring_status(
    ctx: WorkspaceContext = Depends(get_workspace_context), db: AsyncSession = Depends(get_db)
) -> MonitoringStatus:
    since = datetime.now(UTC) - timedelta(hours=24)
    scope = or_(AlertRecord.workspace_id == ctx.workspace.id, AlertRecord.workspace_id.is_(None))
    base = select(func.count(AlertRecord.id)).where(scope, AlertRecord.created_at >= since)
    total = int((await db.execute(base)).scalar_one())
    failed = int((await db.execute(base.where(AlertRecord.status.in_(("failed", "partial"))))).scalar_one())
    return MonitoringStatus(
        readiness=await readiness(),
        channel=channel_status(),  # type: ignore[arg-type]
        channels=channels_status(),  # type: ignore[arg-type]
        alerts_24h=total,
        alerts_failed_24h=failed,
        alerts_enabled=settings.ALERTS_ENABLED,
        metrics_enabled=settings.METRICS_ENABLED,
    )


MAX_ALERTS_PER_WEBHOOK = 20
SEVERITIES = {"critical": "critical", "warning": "warning", "info": "info"}


def alertmanager_alerts(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Alertmanager webhook payload (version 4) -> kwargs of emit_alert (one per alert, bounded)."""
    out: list[dict[str, Any]] = []
    for alert in (body.get("alerts") or [])[:MAX_ALERTS_PER_WEBHOOK]:
        labels = alert.get("labels") or {}
        ann = alert.get("annotations") or {}
        name = str(labels.get("alertname", "alerta"))[:80]
        resolved = alert.get("status") == "resolved"
        text = str(ann.get("summary") or ann.get("description") or name)[:500]
        if ann.get("description") and ann.get("summary"):
            text = f"{text}\n{str(ann['description'])[:500]}"
        if ann.get("runbook"):
            text = f"{text}\nRunbook: {str(ann['runbook'])[:200]}"
        instance = labels.get("instance") or labels.get("job") or labels.get("stream") or labels.get("queue")
        out.append({
            "severity": "info" if resolved else SEVERITIES.get(str(labels.get("severity", "")).lower(), "warning"),
            "dedup": f"am:{name}:{instance}:{'resolved' if resolved else 'firing'}",
            "alertname": name, "text": text, "resolved": resolved,
        })  # fmt: skip
    return out


@router.post("/alertmanager", status_code=202, include_in_schema=False)
async def alertmanager_webhook(request: Request) -> dict[str, int]:
    """Receives Prometheus Alertmanager notifications and forwards them to the alert channel (WhatsApp).

    Disabled (404) unless ALERTMANAGER_WEBHOOK_TOKEN is set; requires `Authorization: Bearer <token>`.
    Caddy also answers 404 for this path: only the internal Docker network reaches it.
    """
    token = settings.ALERTMANAGER_WEBHOOK_TOKEN
    if not token:
        raise HTTPException(status_code=404)
    supplied = request.headers.get("authorization", "")
    if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
        raise HTTPException(status_code=401)
    try:
        body = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="JSON inválido") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="JSON inválido")
    alerts = alertmanager_alerts(body)
    for kwargs in alerts:
        emit_alert(AlertEvent.INFRA_ALERT, **kwargs)
    return {"received": len(alerts)}
