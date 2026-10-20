"""Celery tasks (placeholders)."""

from app.tasks.celery_app import celery_app


@celery_app.task(name="bots.start")
def start_bot(account_id: str) -> dict:
    # TODO: load account, decrypt keys, run strategy loop with RiskManager checks.
    return {"account_id": account_id, "status": "started"}


@celery_app.task(name="bots.stop")
def stop_bot(account_id: str) -> dict:
    return {"account_id": account_id, "status": "stopped"}


@celery_app.task(name="bots.reconcile")
def reconcile(account_id: str) -> dict:
    return {"account_id": account_id, "drift": []}
