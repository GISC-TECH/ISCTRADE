"""Structured logging (JSON lines), request/task correlation and secret redaction."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from app.core.config import settings
from app.core.redaction import redact, redact_text

try:  # faster JSON lines (one per request/task/audit event); stdlib fallback
    import orjson as _orjson
except ImportError:  # pragma: no cover
    _orjson = None

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
task_id_var: ContextVar[str | None] = ContextVar("task_id", default=None)
task_name_var: ContextVar[str | None] = ContextVar("task_name", default=None)

_STD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}


def new_request_id() -> str:
    return uuid.uuid4().hex


def current_context() -> dict[str, str]:
    ctx = {"request_id": request_id_var.get(), "task_id": task_id_var.get(), "task_name": task_name_var.get()}
    return {k: v for k, v in ctx.items() if v}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
            **current_context(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _STD_ATTRS and not k.startswith("_")}
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            payload["exc"] = redact_text(self.formatException(record.exc_info))
        if _orjson is not None:
            try:
                return _orjson.dumps(payload, default=str, option=_orjson.OPT_NON_STR_KEYS).decode()
            except TypeError:  # pragma: no cover - exotic values: stdlib fallback
                pass
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        text = redact_text(super().format(record))
        ctx = current_context()
        return f"{text} [{' '.join(f'{k}={v}' for k, v in ctx.items())}]" if ctx else text


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Idempotent root logging setup used by the API, Celery and the streams worker."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if (fmt or settings.LOG_FORMAT).lower() == "json" else TextFormatter())
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_btp", False):
            root.removeHandler(h)
    handler._btp = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel((level or settings.LOG_LEVEL).upper())
    # Route uvicorn/celery loggers through the root handler; our middleware logs each request.
    for name in ("uvicorn", "uvicorn.error", "celery", "celery.task", "celery.worker"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
    access = logging.getLogger("uvicorn.access")
    access.handlers.clear()
    access.propagate = False
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
