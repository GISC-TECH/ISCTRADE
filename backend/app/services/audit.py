"""Audit log (placeholder). Records security- and trading-relevant events."""
import logging
import uuid
from typing import Any

logger = logging.getLogger("audit")


async def log_event(event: str, *, user_id: uuid.UUID | None = None, **data: Any) -> None:
    # TODO: persist to an append-only `audit_logs` table. Never log secrets.
    logger.info("audit event=%s user=%s data=%s", event, user_id, {k: str(v) for k, v in data.items()})
