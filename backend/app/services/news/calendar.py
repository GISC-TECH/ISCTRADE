"""Economic calendar: ForexFactory weekly JSON (faireconomy mirror) + manual events from configuration."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models.base import utcnow
from app.models.news import EconomicEvent
from app.services.news.gate import EventInfo
from app.services.news.sources import USER_AGENT

logger = logging.getLogger(__name__)

IMPACT_MAP = {"high": "high", "medium": "medium", "low": "low", "holiday": "holiday", "non-economic": "low"}


def _key(source: str, title: str, country: str, when: datetime) -> str:
    raw = f"{source}|{title.strip().lower()}|{country.upper()}|{when.astimezone(UTC).isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_events(rows: list[dict[str, Any]], source: str) -> list[dict[str, Any]]:
    """Normalise ForexFactory-style rows: title, country, date|time (ISO), impact, forecast, previous."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or "").strip()[:255]
        when = _parse_time(row.get("date") or row.get("time"))
        if not title or when is None:
            continue
        country = str(row.get("country") or "").strip().upper()[:10]
        impact = IMPACT_MAP.get(str(row.get("impact") or "low").strip().lower(), "low")
        out.append(
            {
                "source": source,
                "dedup_key": _key(source, title, country, when),
                "title": title,
                "country": country,
                "impact": impact,
                "event_time": when,
                "forecast": (str(row["forecast"])[:50] or None) if row.get("forecast") not in (None, "") else None,
                "previous": (str(row["previous"])[:50] or None) if row.get("previous") not in (None, "") else None,
            }
        )
    return out


def manual_events() -> list[dict[str, Any]]:
    raw = settings.ECON_CALENDAR_MANUAL_EVENTS.strip()
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ECON_CALENDAR_MANUAL_EVENTS is not valid JSON; ignored")
        return []
    return parse_events(rows if isinstance(rows, list) else [], "manual")


async def fetch_forexfactory(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    resp = await client.get(settings.ECON_CALENDAR_URL, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    data = resp.json()
    return parse_events(data if isinstance(data, list) else [], "forexfactory")


async def upsert_events(db: AsyncSession, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    keys = [r["dedup_key"] for r in rows]
    existing = {
        e.dedup_key: e for e in (await db.scalars(select(EconomicEvent).where(EconomicEvent.dedup_key.in_(keys))))
    }
    created = 0
    for r in rows:
        ev = existing.get(r["dedup_key"])
        if ev is None:
            db.add(EconomicEvent(**r, updated_at=utcnow()))
            created += 1
        else:
            ev.impact, ev.forecast, ev.previous, ev.updated_at = r["impact"], r["forecast"], r["previous"], utcnow()
    return created


async def refresh_calendar(sessions: async_sessionmaker[AsyncSession], client: httpx.AsyncClient | None = None) -> dict:
    """Fetch ForexFactory (if enabled) + manual events and upsert them. Errors fall back to manual events."""
    rows = manual_events()
    errors: list[str] = []
    if settings.ECON_CALENDAR_ENABLED and settings.ECON_CALENDAR_URL:
        own = client is None
        http = client or httpx.AsyncClient(timeout=settings.NEWS_HTTP_TIMEOUT_SECONDS, follow_redirects=True)
        try:
            rows += await fetch_forexfactory(http)
        except Exception as exc:
            errors.append(f"forexfactory: {type(exc).__name__}")
            logger.warning("economic calendar fetch failed: %s", type(exc).__name__)
        finally:
            if own:
                await http.aclose()
    async with sessions() as db:
        created = await upsert_events(db, rows)
        await db.commit()
    return {"events": len(rows), "created": created, "errors": errors}


async def load_events(db: AsyncSession, start: datetime, end: datetime) -> list[EventInfo]:
    rows = await db.scalars(
        select(EconomicEvent)
        .where(EconomicEvent.event_time >= start, EconomicEvent.event_time <= end)
        .order_by(EconomicEvent.event_time)
    )
    return [to_info(e) for e in rows]


def to_info(e: EconomicEvent) -> EventInfo:
    t = e.event_time if e.event_time.tzinfo else e.event_time.replace(tzinfo=UTC)
    return EventInfo(str(e.id), e.title, e.country, e.impact, t)


async def events_around(db: AsyncSession, now: datetime, hours: float = 26) -> list[EventInfo]:
    return await load_events(db, now - timedelta(hours=hours), now + timedelta(hours=hours))
