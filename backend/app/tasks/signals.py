"""Celery signal handlers: JSON logging, request-id propagation, task metrics and failure handling."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from celery.signals import (
    before_task_publish,
    setup_logging,
    task_failure,
    task_postrun,
    task_prerun,
    worker_init,
    worker_process_shutdown,
)

from app.core.config import settings
from app.core.logging import configure_logging, request_id_var, task_id_var, task_name_var
from app.core.metrics import CELERY_TASK_DURATION, CELERY_TASKS, start_metrics_server

logger = logging.getLogger("celery.tasks")
_started: dict[str, tuple[float, list[Any]]] = {}


@setup_logging.connect
def _setup_logging(**_: Any) -> None:  # replaces Celery's own logging setup
    configure_logging()


@before_task_publish.connect
def _propagate_request_id(headers: dict | None = None, **_: Any) -> None:
    rid = request_id_var.get()
    if rid and headers is not None:
        headers.setdefault("request_id", rid)


@task_prerun.connect
def _task_prerun(task_id: str | None = None, task: Any = None, **_: Any) -> None:
    rid = getattr(getattr(task, "request", None), "request_id", None) or task_id
    tokens = [request_id_var.set(rid), task_id_var.set(task_id), task_name_var.set(getattr(task, "name", None))]
    if task_id:
        _started[task_id] = (time.perf_counter(), tokens)


@task_postrun.connect
def _task_postrun(task_id: str | None = None, task: Any = None, state: str | None = None, **_: Any) -> None:
    name = getattr(task, "name", "unknown")
    started = _started.pop(task_id, None) if task_id else None
    try:
        CELERY_TASKS.labels(task=name, status=(state or "SUCCESS").lower()).inc()
        if started:
            CELERY_TASK_DURATION.labels(task=name).observe(time.perf_counter() - started[0])
    except Exception:  # pragma: no cover
        pass
    if started:
        for var, token in zip((request_id_var, task_id_var, task_name_var), started[1], strict=False):
            try:
                var.reset(token)
            except ValueError:  # pragma: no cover - different context
                var.set(None)


@task_failure.connect
def _task_failure(sender: Any = None, task_id: str | None = None, exception: BaseException | None = None, **_: Any):
    name = getattr(sender, "name", "unknown")
    logger.error("task failed", extra={"task": name, "error": type(exception).__name__ if exception else None})
    on_task_failure(name, task_id, exception)


def on_task_failure(name: str, task_id: str | None, exception: BaseException | None) -> None:
    """Extension point (alerts are attached in app.services.alerts)."""


@worker_init.connect
def _worker_init(**_: Any) -> None:
    start_metrics_server(settings.METRICS_PORT)


@worker_process_shutdown.connect
def _worker_process_shutdown(pid: int | None = None, **_: Any) -> None:
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        from prometheus_client import multiprocess

        multiprocess.mark_process_dead(pid or os.getpid())
