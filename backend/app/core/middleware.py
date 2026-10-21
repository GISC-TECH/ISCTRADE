"""ASGI middleware: request IDs (X-Request-ID), structured access log and HTTP metrics."""

from __future__ import annotations

import logging
import re
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import new_request_id, request_id_var
from app.core.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = logging.getLogger("http")
_VALID_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SKIP_LOG = {"/health", "/metrics"}


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_ID.match(incoming) else new_request_id()
        token = request_id_var.set(request_id)
        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration = time.perf_counter() - start
            route = scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            method = scope.get("method", "GET")
            try:
                HTTP_REQUESTS.labels(method, template, str(status_code)).inc()
                HTTP_LATENCY.labels(method, template).observe(duration)
            except Exception:  # pragma: no cover
                pass
            if scope.get("path") not in _SKIP_LOG:
                logger.info(
                    "%s %s %s",
                    method,
                    template,
                    status_code,
                    extra={
                        "http_status": status_code,
                        "duration_ms": round(duration * 1000, 1),
                        "path": scope.get("path"),
                    },
                )
            request_id_var.reset(token)
