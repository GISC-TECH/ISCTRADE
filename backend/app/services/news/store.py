"""News pipeline: fetch -> dedup -> tag -> store -> analyse -> alert; plus queries used by bots and the API."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.redis import get_redis
from app.models import BotConfig, BotState, Position
from app.models.base import utcnow
from app.models.news import EconomicEvent, NewsItem
from app.schemas.bot_options import BotOptions
from app.services.alerts import queue_alert
from app.services.alerts.events import AlertEvent
from app.services.news import sentiment, sources, tagging
from app.services.news.gate import NewsSignal, aggregate

logger = logging.getLogger(__name__)

THROTTLE_PREFIX = "news:last_fetch:"
_memory_last: dict[str, float] = {}
ACTIVE_BOT_STATES = (BotState.RUNNING, BotState.PAUSED, BotState.STARTING)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def normalise_url(url: str | None) -> str | None:
    if not url:
        return None
    parts = urlsplit(url.strip())
    if not parts.netloc:
        return None
    return urlunsplit(
        (parts.scheme.lower() or "https", parts.netloc.lower().removeprefix("www."), parts.path.rstrip("/"), "", "")
    )


def dedup_key(raw: sources.RawNews) -> str:
    url = normalise_url(raw.url)
    basis = f"url:{url}" if url else f"title:{re.sub(r'[^a-z0-9]+', ' ', raw.title.lower()).strip()}"
    return hashlib.sha256(basis.encode()).hexdigest()


async def _due(source: str, min_interval: int) -> bool:
    """Throttle paid/limited sources (CryptoPanic quota, X rate limits)."""
    if min_interval <= 0:
        return True
    key = THROTTLE_PREFIX + source
    try:
        ok = await asyncio.wait_for(get_redis().set(key, "1", ex=min_interval, nx=True), timeout=1)
        return bool(ok)
    except Exception:
        now = time.monotonic()
        if now - _memory_last.get(source, -1e12) < min_interval:
            return False
        _memory_last[source] = now
        return True


async def fetch_all(client: httpx.AsyncClient) -> tuple[list[sources.RawNews], list[str]]:
    jobs: list[tuple[str, asyncio.Future | object]] = []
    for url in settings.NEWS_RSS_FEEDS:
        jobs.append((sources.source_name_for(url), sources.fetch_rss(client, url)))
    if settings.CRYPTOPANIC_API_TOKEN and await _due("cryptopanic", settings.CRYPTOPANIC_MIN_INTERVAL_SECONDS):
        jobs.append(("cryptopanic", sources.fetch_cryptopanic(client)))
    if settings.X_BEARER_TOKEN and sources.build_x_query() and await _due("x", settings.X_MIN_INTERVAL_SECONDS):
        jobs.append(("x", sources.fetch_x(client)))
    results = await asyncio.gather(*(j for _, j in jobs), return_exceptions=True)  # type: ignore[arg-type]
    items: list[sources.RawNews] = []
    errors: list[str] = []
    for (name, _), res in zip(jobs, results, strict=True):
        if isinstance(res, BaseException):
            # no URL in the message: CryptoPanic carries the token in the query string
            errors.append(f"{name}: {type(res).__name__}")
            logger.warning("news source failed source=%s error=%s", name, type(res).__name__)
        else:
            items.extend(res)
    return items, errors


async def _universe(db: AsyncSession) -> set[str] | None:
    from app.services.scanner import latest_scan

    scan = await latest_scan(db)
    if scan is None or not scan.results:
        return None
    return {r["symbol"] for r in scan.results if r.get("symbol")}


async def store_items(db: AsyncSession, items: list[sources.RawNews]) -> list[NewsItem]:
    by_key: dict[str, sources.RawNews] = {}
    for raw in items:
        by_key.setdefault(dedup_key(raw), raw)
    if not by_key:
        return []
    existing = set(await db.scalars(select(NewsItem.dedup_key).where(NewsItem.dedup_key.in_(list(by_key)))))
    universe = await _universe(db)
    cutoff = datetime.now(UTC) - timedelta(days=settings.NEWS_RETENTION_DAYS)
    created = []
    for key, raw in by_key.items():
        if key in existing or raw.published_at < cutoff:
            continue
        assets, symbols = tagging.tag(f"{raw.title} {raw.body[:500]}", raw.assets_hint, universe)
        item = NewsItem(
            source=raw.source, source_name=raw.source_name[:120], external_id=raw.external_id, dedup_key=key,
            url=(raw.url or None) and raw.url[:1000], title=raw.title[:500], body=raw.body[:2000],
            published_at=raw.published_at, assets=assets, symbols=symbols, extra=raw.extra,
        )  # fmt: skip
        db.add(item)
        created.append(item)
    await db.flush()
    return created


async def analyse_pending(db: AsyncSession, client: httpx.AsyncClient | None = None, limit: int = 200) -> int:
    pending = list(
        await db.scalars(
            select(NewsItem).where(NewsItem.analyzed_at.is_(None)).order_by(NewsItem.published_at.desc()).limit(limit)
        )
    )
    universe = await _universe(db) if pending else None
    llm_left = settings.LLM_MAX_ITEMS_PER_RUN if sentiment.llm_enabled() else 0
    for item in pending:
        if llm_left > 0:
            llm_left -= 1
            res = await sentiment.analyse(item.title, item.body, item.source_name, client)
        else:
            res = sentiment.lexicon_sentiment(item.title, item.body)
        item.sentiment, item.impact, item.analysis_method = res.sentiment, res.impact, res.method
        item.summary_pt = res.summary_pt
        item.analyzed_at = utcnow()
        if res.assets:
            assets, symbols = tagging.tag("", [*item.assets, *res.assets], universe)
            item.assets, item.symbols = assets, symbols
    return len(pending)


async def traded_symbols(db: AsyncSession) -> dict[uuid.UUID, set[str]]:
    """Symbols each workspace is exposed to: active bots (incl. auto-selected) and open positions."""
    out: dict[uuid.UUID, set[str]] = defaultdict(set)
    for bot in await db.scalars(select(BotConfig).where(BotConfig.state.in_(ACTIVE_BOT_STATES))):
        if BotOptions.load(bot.options).auto_symbols.enabled:
            out[bot.workspace_id].update((bot.runtime or {}).get("auto") or [])
        else:
            out[bot.workspace_id].add(bot.symbol)
    for pos in await db.scalars(select(Position).where(Position.quantity != 0)):
        out[pos.workspace_id].add(pos.symbol)
    return out


async def alert_high_impact(db: AsyncSession, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    candidates = list(
        await db.scalars(
            select(NewsItem).where(
                NewsItem.alerted_at.is_(None),
                NewsItem.impact == "high",
                NewsItem.published_at >= now - timedelta(hours=6),
            )
        )
    )
    candidates = [c for c in candidates if abs(c.sentiment or 0) >= settings.NEWS_ALERT_MIN_ABS_SENTIMENT]
    if not candidates:
        return 0
    exposure = await traded_symbols(db)
    sent = 0
    for item in candidates:
        item.alerted_at = utcnow()
        for ws_id, symbols in exposure.items():
            hit = sorted(set(item.symbols or []) & symbols)
            if not hit:
                continue
            queue_alert(
                db, AlertEvent.NEWS_HIGH_IMPACT, workspace_id=ws_id, dedup=f"news:{item.id}",
                severity="warning", symbol=", ".join(hit), title=item.title,
                sentiment=round(item.sentiment or 0, 2), summary=item.summary_pt, source=item.source_name, url=item.url,
            )  # fmt: skip
            sent += 1
    return sent


async def alert_upcoming_events(db: AsyncSession, now: datetime | None = None) -> int:
    """Warn workspaces with active bots about high-impact events starting within ECON_ALERT_LEAD_MINUTES."""
    now = now or datetime.now(UTC)
    countries = {c.upper() for c in settings.ECON_CALENDAR_COUNTRIES}
    events = [
        e
        for e in await db.scalars(
            select(EconomicEvent).where(
                EconomicEvent.alerted_at.is_(None),
                EconomicEvent.impact == "high",
                EconomicEvent.event_time > now,
                EconomicEvent.event_time <= now + timedelta(minutes=settings.ECON_ALERT_LEAD_MINUTES),
            )
        )
        if not countries or e.country.upper() in countries
    ]
    if not events:
        return 0
    workspaces = set(await db.scalars(select(BotConfig.workspace_id).where(BotConfig.state.in_(ACTIVE_BOT_STATES))))
    n = 0
    for ev in events:
        ev.alerted_at = utcnow()
        minutes = int((_aware(ev.event_time) - now).total_seconds() // 60)
        for ws_id in workspaces:
            queue_alert(
                db, AlertEvent.ECONOMIC_EVENT, workspace_id=ws_id, dedup=f"econ:{ev.id}", title=ev.title,
                country=ev.country, time=_aware(ev.event_time).strftime("%d/%m %H:%M UTC"), minutes=minutes,
                forecast=ev.forecast, previous=ev.previous,
            )  # fmt: skip
            n += 1
    return n


async def prune(db: AsyncSession) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=settings.NEWS_RETENTION_DAYS)
    await db.execute(delete(NewsItem).where(NewsItem.published_at < cutoff))
    await db.execute(delete(EconomicEvent).where(EconomicEvent.event_time < cutoff - timedelta(days=30)))


async def ingest(sessions: async_sessionmaker[AsyncSession], client: httpx.AsyncClient | None = None) -> dict:
    """Full news cycle used by the Celery task `news.ingest`."""
    own = client is None
    http = client or httpx.AsyncClient(timeout=settings.NEWS_HTTP_TIMEOUT_SECONDS, follow_redirects=True)
    try:
        items, errors = await fetch_all(http) if settings.NEWS_ENABLED else ([], [])
        async with sessions() as db:
            created = await store_items(db, items)
            await db.commit()
        async with sessions() as db:
            analysed = await analyse_pending(db, http)
            await db.commit()
    finally:
        if own:
            await http.aclose()
    async with sessions() as db:
        alerts = await alert_high_impact(db)
        await prune(db)
        await db.commit()
    if created:
        from app.services import realtime

        latest = [{"id": i.id, "title": i.title, "symbols": i.symbols, "source": i.source_name} for i in created[:10]]
        await realtime.publish(realtime.GLOBAL, realtime.message("news", {"created": len(created), "latest": latest}))
    return {"fetched": len(items), "created": len(created), "analysed": analysed, "alerts": alerts, "errors": errors}


# --------------------------------------------------------------------------- queries


async def load_signals(db: AsyncSession, start: datetime, end: datetime, symbol: str | None = None) -> list[NewsSignal]:
    rows = await db.scalars(
        select(NewsItem)
        .where(NewsItem.published_at >= start, NewsItem.published_at <= end, NewsItem.sentiment.is_not(None))
        .order_by(NewsItem.published_at.desc())
        .limit(5000)
    )
    out = []
    for r in rows:
        if symbol and symbol not in (r.symbols or []):
            continue
        out.append(
            NewsSignal(
                _aware(r.published_at), float(r.sentiment or 0), r.impact or "low", tuple(r.symbols or []), r.title
            )
        )
    return out


async def sentiment_overview(
    db: AsyncSession, symbols: list[str] | None = None, now: datetime | None = None
) -> list[dict]:
    now = now or datetime.now(UTC)
    signals = await load_signals(db, now - timedelta(hours=settings.NEWS_SENTIMENT_WINDOW_HOURS), now)
    if symbols is None:
        counts: dict[str, int] = defaultdict(int)
        for s in signals:
            for sym in s.symbols:
                counts[sym] += 1
        symbols = [s for s, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:20]]
    out = []
    for sym in symbols:
        agg = aggregate(
            signals, sym, now, settings.NEWS_SENTIMENT_HALF_LIFE_HOURS, settings.NEWS_SENTIMENT_WINDOW_HOURS
        )
        out.append(agg.as_dict())
    return out
