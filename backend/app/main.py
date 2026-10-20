"""FastAPI application entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.redis import close_redis


def check_production_settings() -> None:
    if settings.is_production and (problems := settings.production_problems()):
        raise RuntimeError("Configuração de produção inválida: " + "; ".join(problems))


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_production_settings()
    yield
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

app.include_router(api_router, prefix=settings.API_V1_PREFIX)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok", "environment": settings.ENVIRONMENT}
