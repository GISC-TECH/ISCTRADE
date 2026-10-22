"""Audit trail hash chain tasks (queue: default): seal new rows, verify the whole chain daily."""

from __future__ import annotations

import logging
from typing import Any

from app.services import audit_chain
from app.tasks.celery_app import celery_app
from app.tasks.runtime import run_async

logger = logging.getLogger(__name__)


async def _seal(sessions) -> dict[str, Any]:
    async with sessions() as db:
        return await audit_chain.seal_pending(db)


async def _verify(sessions) -> dict[str, Any]:
    from app.services.audit import log_event

    async with sessions() as db:
        await audit_chain.seal_pending(db)
        report = await audit_chain.verify_chain(db)
        if not report.ok:
            logger.error("audit chain BROKEN: %s", report.errors[:5])
            await log_event(db, "audit.chain_broken", level="critical", commit=True, errors=report.errors[:10])
        return report.as_dict()


@celery_app.task(name="audit.seal", soft_time_limit=100, time_limit=150)
def seal() -> dict[str, Any]:
    return run_async(_seal)


@celery_app.task(name="audit.verify", soft_time_limit=1500, time_limit=1800)
def verify() -> dict[str, Any]:
    return run_async(_verify)
