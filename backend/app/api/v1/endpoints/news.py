"""News feed, per-asset sentiment and economic calendar (read-only; global market data)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context, require_workspace_role
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiting import rate_limiter
from app.core.security import Role
from app.models.news import EconomicEvent, NewsItem
from app.services.dispatch import enqueue
from app.services.news.sentiment import llm_enabled
from app.services.news.sources import build_x_query, source_name_for
from app.services.news.store import sentiment_overview

router = APIRouter()
calendar_router = APIRouter()


class NewsItemRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    source_name: str
    url: str | None
    title: str
    published_at: datetime
    assets: list[str]
    symbols: list[str]
    sentiment: float | None
    impact: str | None
    summary_pt: str | None
    analysis_method: str | None


class NewsPage(BaseModel):
    items: list[NewsItemRead]
    total: int
    limit: int
    offset: int


class NewsStatus(BaseModel):
    enabled: bool
    llm_enabled: bool
    llm_model: str | None
    rss_sources: list[str]
    cryptopanic_enabled: bool
    x_enabled: bool
    last_item_at: datetime | None
    total_items: int
    half_life_hours: float
    window_hours: float


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source: str
    title: str
    country: str
    impact: str
    event_time: datetime
    forecast: str | None
    previous: str | None


@router.get("/", response_model=NewsPage)
async def list_news(
    _: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    symbol: str | None = Query(None, max_length=30),
    source: str | None = Query(None, max_length=50),
    impact: Literal["low", "medium", "high"] | None = None,
    sentiment: Literal["positive", "negative", "neutral"] | None = None,
    search: str | None = Query(None, max_length=100),
    hours: int = Query(72, ge=1, le=24 * 30),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> NewsPage:
    """Notícias recentes com sentimento (filtro, não sinal de entrada)."""
    q = select(NewsItem).where(NewsItem.published_at >= datetime.now(UTC) - timedelta(hours=hours))
    if source:
        q = q.where(NewsItem.source == source)
    if impact:
        q = q.where(NewsItem.impact == impact)
    if sentiment == "positive":
        q = q.where(NewsItem.sentiment >= 0.15)
    elif sentiment == "negative":
        q = q.where(NewsItem.sentiment <= -0.15)
    elif sentiment == "neutral":
        q = q.where(NewsItem.sentiment > -0.15, NewsItem.sentiment < 0.15)
    if search:
        like = f"%{search.strip()}%"
        q = q.where(or_(NewsItem.title.ilike(like), NewsItem.summary_pt.ilike(like)))
    rows = list(await db.scalars(q.order_by(NewsItem.published_at.desc()).limit(2000)))
    if symbol:  # JSON membership filtered in Python (portable across PostgreSQL/SQLite)
        sym = symbol.upper()
        rows = [r for r in rows if sym in (r.symbols or [])]
    page = rows[offset : offset + limit]
    return NewsPage(items=[NewsItemRead.model_validate(r) for r in page], total=len(rows), limit=limit, offset=offset)


@router.get("/sentiment")
async def news_sentiment(
    _: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    symbols: str | None = Query(None, max_length=500, description="Lista separada por vírgula (ex.: BTCUSDT,ETHUSDT)"),
) -> list[dict]:
    """Sentimento agregado por ativo (média ponderada por impacto com decaimento exponencial no tempo)."""
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()][:30] if symbols else None
    return await sentiment_overview(db, wanted)


@router.get("/status", response_model=NewsStatus)
async def news_status(
    _: WorkspaceContext = Depends(get_workspace_context), db: AsyncSession = Depends(get_db)
) -> NewsStatus:
    """Fontes ativas (sem expor tokens)."""
    last = await db.scalar(select(func.max(NewsItem.published_at)))
    total = await db.scalar(select(func.count()).select_from(NewsItem)) or 0
    return NewsStatus(
        enabled=settings.NEWS_ENABLED,
        llm_enabled=llm_enabled(),
        llm_model=settings.LLM_MODEL if llm_enabled() else None,
        rss_sources=[source_name_for(u) for u in settings.NEWS_RSS_FEEDS],
        cryptopanic_enabled=bool(settings.CRYPTOPANIC_API_TOKEN),
        x_enabled=bool(settings.X_BEARER_TOKEN and build_x_query()),
        last_item_at=last,
        total_items=total,
        half_life_hours=settings.NEWS_SENTIMENT_HALF_LIFE_HOURS,
        window_hours=settings.NEWS_SENTIMENT_WINDOW_HOURS,
    )


@router.post("/refresh", status_code=202, dependencies=[Depends(rate_limiter(3, scope="news-refresh"))])
async def refresh_news(_: WorkspaceContext = Depends(require_workspace_role(Role.TRADER))) -> dict[str, bool]:
    """Agenda uma coleta de notícias (worker Celery)."""
    return {"queued": enqueue("news.ingest")}


@calendar_router.get("/", response_model=list[EventRead])
async def list_events(
    _: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    days_back: int = Query(1, ge=0, le=30),
    days_ahead: int = Query(7, ge=0, le=30),
    impact: Literal["low", "medium", "high"] | None = None,
    country: str | None = Query(None, max_length=10),
) -> list[EconomicEvent]:
    """Calendário econômico (ForexFactory + eventos manuais)."""
    now = datetime.now(UTC)
    q = select(EconomicEvent).where(
        EconomicEvent.event_time >= now - timedelta(days=days_back),
        EconomicEvent.event_time <= now + timedelta(days=days_ahead),
    )
    if impact:
        q = q.where(EconomicEvent.impact == impact)
    if country:
        q = q.where(EconomicEvent.country == country.upper())
    return list(await db.scalars(q.order_by(EconomicEvent.event_time).limit(1000)))


@calendar_router.post("/refresh", status_code=202, dependencies=[Depends(rate_limiter(3, scope="calendar-refresh"))])
async def refresh_events(_: WorkspaceContext = Depends(require_workspace_role(Role.TRADER))) -> dict[str, bool]:
    """Agenda a atualização do calendário econômico."""
    return {"queued": enqueue("calendar.refresh")}
