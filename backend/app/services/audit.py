"""Audit trail service: persists events to `audit_logs` (append-only) and mirrors them to the log."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog

logger = logging.getLogger("audit")

_SENSITIVE = ("secret", "password", "api_key", "token", "signature")


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(k): ("***" if any(s in str(k).lower() for s in _SENSITIVE) else _sanitize(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(v) for v in value]
    if isinstance(value, (uuid.UUID, Decimal)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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
    if db is None:
        from app.core.database import session_scope

        async with session_scope() as session:
            session.add(entry)
            await session.commit()
        return entry
    db.add(entry)
    if commit:
        await db.commit()
    return entry
