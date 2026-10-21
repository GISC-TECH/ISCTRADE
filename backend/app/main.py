"""FastAPI application entrypoint."""

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.logging import configure_logging
from app.core.metrics import DB_COLLECTOR, api_registry, register_db_collector
from app.core.middleware import RequestContextMiddleware
from app.core.redis import close_redis
from app.services import health as health_service

configure_logging()
register_db_collector()
logger = logging.getLogger(__name__)


def check_production_settings() -> None:
    if settings.is_production and (problems := settings.production_problems()):
        raise RuntimeError("Configuração de produção inválida: " + "; ".join(problems))


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_production_settings()
    yield
    from app.services.binance_http import close_shared_clients

    await close_shared_clients()
    await close_redis()


# Documentação interativa desabilitada em produção (a menos que DEBUG=true).
_docs = not settings.is_production or settings.DEBUG
app = FastAPI(
    title=settings.PROJECT_NAME,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/docs" if _docs else None,
    redoc_url="/redoc" if _docs else None,
    openapi_url="/openapi.json" if _docs else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(RequestContextMiddleware)

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Liveness: the process is up (no dependency checks)."""
    return {"status": "ok", "environment": settings.ENVIRONMENT}


@app.get("/health/ready", tags=["health"])
async def ready() -> JSONResponse:
    """Readiness: database, Redis and Celery broker (critical) + streams worker heartbeat (degrades only)."""
    result = await health_service.readiness()
    return JSONResponse(result, status_code=200 if result["ready"] else 503)


@app.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    """Prometheus metrics. Not routed by Caddy; optionally protected by METRICS_TOKEN (Bearer)."""
    if not settings.METRICS_ENABLED:
        return Response(status_code=404)
    if settings.METRICS_TOKEN:
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), f"Bearer {settings.METRICS_TOKEN}".encode()):
            return Response(status_code=401, headers={"WWW-Authenticate": "Bearer"})
    from app.services.metrics_snapshot import collect

    try:
        async with AsyncSessionLocal() as db:
            DB_COLLECTOR.snapshot = await asyncio.wait_for(collect(db), timeout=5)
    except Exception:
        logger.warning("metrics snapshot failed", exc_info=True)
    return Response(generate_latest(api_registry()), media_type=CONTENT_TYPE_LATEST)
