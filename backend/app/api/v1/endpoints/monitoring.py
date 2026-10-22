"""Monitoring status for the dashboard (readiness + alert channel, no secrets)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context
from app.core.config import settings
from app.core.database import get_db
from app.models.alert import AlertRecord
from app.schemas.alert import MonitoringStatus
from app.services.alerts.service import channels_status
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
