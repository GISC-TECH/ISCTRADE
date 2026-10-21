"""Audit trail service: persists events to `audit_logs` (append-only) and mirrors them to the log."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redaction import redact
from app.models.audit import AuditLog

logger = logging.getLogger("audit")


def _sanitize(value: Any) -> Any:
    """Mask sensitive keys and scrub secret-looking strings (see app.core.redaction)."""
    return redact(value)


async def log_event(
    db: AsyncSession | None,
    event: str,
    *,
    user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    ip_address: str | None = None,
    level: str = "info",
    commit: bool = False,
    **data: Any,
) -> AuditLog | None:
    """Record an audit event.

    The row is added to `db` (the caller's transaction) so it is committed atomically with the
    business change. Pass `commit=True` to commit immediately. With `db=None` a new session is opened.
    """
    clean = _sanitize(data)
    logger.log(
        logging.WARNING if level in ("warning", "error", "critical") else logging.INFO,
        "audit event=%s user=%s workspace=%s account=%s data=%s",
        event,
        user_id,
        workspace_id,
        account_id,
        clean,
    )
    entry = AuditLog(
        event=event,
        level=level,
        user_id=user_id,
        workspace_id=workspace_id,
        account_id=account_id,
        ip_address=ip_address,
        data=clean,
    )
    from app.services.alerts.service import on_audit_event  # lazy: avoids an import cycle

    if db is None:
        from app.core.database import session_scope

        async with session_scope() as session:
            session.add(entry)
            on_audit_event(session, event, workspace_id=workspace_id, account_id=account_id, data=clean)
            await session.commit()
        return entry
    db.add(entry)
    # Alerts derived from this event are dispatched only after the transaction commits.
    on_audit_event(db, event, workspace_id=workspace_id, account_id=account_id, data=clean)
    if commit:
        await db.commit()
    return entry
