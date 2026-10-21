"""Paper order book task (resting limits, SL/TP against the mark price)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.services.paper_orders import process_paper_orders
from app.tasks.bot_tasks import run_async
from app.tasks.celery_app import celery_app


async def _process(sessions) -> dict[str, Any]:
    async with sessions() as db:
        return asdict(await process_paper_orders(db))


@celery_app.task(name="paper.process_orders", soft_time_limit=30, time_limit=45)
def process_orders() -> dict[str, Any]:
    return run_async(_process)
