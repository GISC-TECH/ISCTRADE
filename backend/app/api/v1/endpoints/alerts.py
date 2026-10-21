"""Alert settings, history and test endpoint (per workspace)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context, require_workspace_role
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiting import rate_limiter
from app.core.redaction import mask_phone
from app.core.security import Role, has_role
from app.models.alert import AlertRecord, AlertSettings
from app.schemas.alert import AlertEventInfo, AlertRead, AlertSettingsRead, AlertSettingsUpdate, AlertTestRequest
from app.schemas.common import Page
from app.services.alerts import service
from app.services.alerts.events import CONFIGURABLE_EVENTS, EVENTS, AlertEvent, effective_toggles
from app.services.alerts.whatsapp import channel_status
from app.services.audit import log_event

router = APIRouter()


def session_factory_from(db: AsyncSession):
    """Adapter so the worker-side service functions can run on the request's session."""

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield db

    return factory


def _settings_read(row: AlertSettings | None, ctx: WorkspaceContext) -> AlertSettingsRead:
    recipients = list(row.recipients) if row and row.recipients else []
    if not has_role(ctx.role, Role.ADMIN):  # viewers/traders see masked numbers only
        recipients = [mask_phone(r) for r in recipients]
    return AlertSettingsRead(
        enabled=row.enabled if row else True,
        recipients=recipients,
        events=effective_toggles(row.events if row else None),
        min_severity=row.min_severity if row else "info",  # type: ignore[arg-type]
        throttle_minutes=row.throttle_minutes
        if row and row.throttle_minutes is not None
        else settings.ALERT_THROTTLE_MINUTES,
        uses_default_recipients=not recipients,
        available_events=[
            AlertEventInfo(
                key=k,
                label=EVENTS[k].label,
                severity=EVENTS[k].severity.value,  # type: ignore[arg-type]
                default_enabled=EVENTS[k].enabled_by_default,
            )
            for k in CONFIGURABLE_EVENTS
        ],
        channel=channel_status(),  # type: ignore[arg-type]
    )


@router.get("/settings", response_model=AlertSettingsRead)
async def get_alert_settings(
    ctx: WorkspaceContext = Depends(get_workspace_context), db: AsyncSession = Depends(get_db)
) -> AlertSettingsRead:
    return _settings_read(await service.get_settings_row(db, ctx.workspace.id), ctx)


@router.put("/settings", response_model=AlertSettingsRead)
async def update_alert_settings(
    data: AlertSettingsUpdate,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> AlertSettingsRead:
    row = await service.get_settings_row(db, ctx.workspace.id)
    if row is None:
        row = AlertSettings(workspace_id=ctx.workspace.id, enabled=True, recipients=[], events={}, min_severity="info")
        db.add(row)
    changes = data.model_dump(exclude_unset=True)
    if "enabled" in changes and data.enabled is not None:
        row.enabled = data.enabled
    if "recipients" in changes and data.recipients is not None:
        row.recipients = data.recipients
    if "events" in changes and data.events is not None:
        row.events = {**(row.events or {}), **data.events}
    if "min_severity" in changes and data.min_severity is not None:
        row.min_severity = data.min_severity
    if "throttle_minutes" in changes:
        row.throttle_minutes = data.throttle_minutes
    audit = {k: v for k, v in changes.items() if k != "recipients"}
    if "recipients" in changes:
        audit["recipients"] = [mask_phone(r) for r in data.recipients or []]
    await log_event(db, "alerts.settings_updated", user_id=ctx.user.id, workspace_id=ctx.workspace.id, **audit)
    await db.commit()
    await db.refresh(row)
    return _settings_read(row, ctx)


@router.post(
    "/test",
    response_model=AlertRead,
    dependencies=[Depends(rate_limiter(5, scope="alerts-test"))],
)
async def send_test_alert(
    data: AlertTestRequest | None = None,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> AlertRecord:
    """Envia (de forma síncrona) um alerta de teste para os destinatários do workspace."""
    text = (data.message if data and data.message else None) or (
        "Se você recebeu esta mensagem, os alertas via WhatsApp estão funcionando."
    )
    payload = service.build_payload(AlertEvent.TEST, workspace_id=ctx.workspace.id, text=text, actor=ctx.user.email)
    sessions = session_factory_from(db)
    result = await service.process_alert(sessions, payload, force=True)
    alert_id = result.get("alert_id")
    if alert_id is None:  # pragma: no cover - force=True always persists
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Falha ao criar alerta")
    if result["status"] == "pending":
        await service.deliver_alert(sessions, alert_id, final=True)
    record = await db.get(AlertRecord, uuid.UUID(alert_id))
    await db.refresh(record)
    return record  # type: ignore[return-value]


@router.get("/", response_model=Page[AlertRead])
async def list_alerts(
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    event: str | None = None,
    status_filter: str | None = Query(None, alias="status"),
) -> Page[AlertRead]:
    """Alertas do workspace + alertas globais (kill switch global, falhas de tarefas, WS de mercado)."""
    where = [or_(AlertRecord.workspace_id == ctx.workspace.id, AlertRecord.workspace_id.is_(None))]
    if event:
        where.append(AlertRecord.event == event)
    if status_filter:
        where.append(AlertRecord.status == status_filter)
    total = int((await db.execute(select(func.count(AlertRecord.id)).where(*where))).scalar_one())
    rows = await db.scalars(
        select(AlertRecord).where(*where).order_by(AlertRecord.created_at.desc()).limit(limit).offset(offset)
    )
    return Page[AlertRead](items=[AlertRead.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)
