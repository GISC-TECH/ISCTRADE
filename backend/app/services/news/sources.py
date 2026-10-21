"""News sources: RSS/Atom feeds, CryptoPanic (optional) and X recent search (optional).

Every fetcher returns :class:`RawNews` items and never raises for a single broken source; errors are
returned as short strings without URLs/tokens (some sources carry the token in the query string).
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from defusedxml import ElementTree as SafeET

from app.core.config import settings

logger = logging.getLogger(__name__)

USER_AGENT = "ISCTRADE-news/1.0 (+self-hosted)"
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass
class RawNews:
    source: str  # rss | cryptopanic | x
    source_name: str
    title: str
    published_at: datetime
    url: str | None = None
    body: str = ""
    external_id: str | None = None
    assets_hint: list[str] = field(default_factory=list)  # tickers provided by the source (CryptoPanic)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.url = safe_url(self.url)


def safe_url(url: str | None) -> str | None:
    """Only absolute http(s) links are kept: feed content is untrusted (javascript:/data: URLs -> XSS)."""
    if not url:
        return None
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc or len(url) > 2048:
        return None
    return url


def clean_text(value: str | None, limit: int = 2000) -> str:
    text = html.unescape(_TAG.sub(" ", value or ""))
    return _WS.sub(" ", text).strip()[:limit]


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(node: Any, *names: str) -> str | None:
    for child in node:
        if _local(child.tag) in names and (child.text or "").strip():
            return child.text
    return None


def _atom_link(node: Any) -> str | None:
    for child in node:
        if _local(child.tag) == "link":
            rel = child.attrib.get("rel", "alternate")
            if rel == "alternate" and child.attrib.get("href"):
                return child.attrib["href"]
            if (child.text or "").strip():
                return child.text.strip()
    return None


def source_name_for(url: str) -> str:
    host = urlparse(url).hostname or url
    return host.removeprefix("www.")


def parse_feed(xml_text: str, feed_url: str, limit: int | None = None) -> list[RawNews]:
    """Parse RSS 2.0 / Atom safely (defusedxml blocks entity expansion and external entities)."""
    root = SafeET.fromstring(xml_text.encode() if isinstance(xml_text, str) else xml_text)
    name = source_name_for(feed_url)
    entries = [el for el in root.iter() if _local(el.tag) in ("item", "entry")]
    out: list[RawNews] = []
    now = datetime.now(UTC)
    for el in entries[: limit or settings.NEWS_MAX_ITEMS_PER_SOURCE]:
        title = clean_text(_child_text(el, "title"), 500)
        if not title:
            continue
        link = _atom_link(el)
        body = clean_text(_child_text(el, "description", "summary", "content", "encoded"))
        published = _parse_date(_child_text(el, "pubdate", "published", "updated", "date")) or now
        guid = _child_text(el, "guid", "id")
        out.append(
            RawNews(
                source="rss", source_name=name, title=title, url=link, body=body,
                published_at=min(published, now), external_id=(guid or link or "").strip()[:255] or None,
            )
        )  # fmt: skip
    return out


async def fetch_rss(client: httpx.AsyncClient, url: str) -> list[RawNews]:
    resp = await client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, */*"})
    resp.raise_for_status()
    return parse_feed(resp.text, url)


async def fetch_cryptopanic(client: httpx.AsyncClient) -> list[RawNews]:
    if not settings.CRYPTOPANIC_API_TOKEN:
        return []
    params = {"auth_token": settings.CRYPTOPANIC_API_TOKEN, "public": "true", "kind": "news"}
    resp = await client.get(settings.CRYPTOPANIC_API_URL, params=params, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    out: list[RawNews] = []
    now = datetime.now(UTC)
    for post in (resp.json() or {}).get("results", [])[: settings.NEWS_MAX_ITEMS_PER_SOURCE]:
        title = clean_text(post.get("title"), 500)
        if not title:
            continue
        src = post.get("source") or {}
        assets = [str(c.get("code", "")).upper() for c in post.get("currencies") or [] if c.get("code")]
        votes = post.get("votes") or {}
        out.append(
            RawNews(
                source="cryptopanic", source_name=str(src.get("title") or src.get("domain") or "CryptoPanic")[:120],
                title=title, url=post.get("original_url") or post.get("url"),
                body=clean_text(post.get("description")),
                published_at=min(_parse_date(post.get("published_at")) or now, now),
                external_id=str(post.get("id")) if post.get("id") is not None else None, assets_hint=assets,
                extra={"votes": {k: votes.get(k) for k in ("positive", "negative", "important") if k in votes}},
            )
        )  # fmt: skip
    return out


def build_x_query() -> str | None:
    parts = [f"from:{a.lstrip('@')}" for a in settings.X_ACCOUNTS if a.strip()]
    parts += [f"({q})" for q in settings.X_QUERIES if q.strip()]
    if not parts:
        return None
    query = " OR ".join(parts)
    if "is:retweet" not in query:
        query = f"({query}) -is:retweet"
    return query[:512]


async def fetch_x(client: httpx.AsyncClient) -> list[RawNews]:
    query = build_x_query()
    if not settings.X_BEARER_TOKEN or not query:
        return []
    params = {
        "query": query,
        "max_results": str(max(10, min(100, settings.X_MAX_RESULTS))),
        "tweet.fields": "created_at,author_id,lang",
        "expansions": "author_id",
        "user.fields": "username",
    }
    headers = {"Authorization": f"Bearer {settings.X_BEARER_TOKEN}", "User-Agent": USER_AGENT}
    resp = await client.get(settings.X_API_URL, params=params, headers=headers)
    resp.raise_for_status()
    data = resp.json() or {}
    users = {u.get("id"): u.get("username") for u in (data.get("includes") or {}).get("users", [])}
    now = datetime.now(UTC)
    out: list[RawNews] = []
    for tw in data.get("data") or []:
        text = clean_text(tw.get("text"), 1000)
        if not text:
            continue
        user = users.get(tw.get("author_id")) or "x"
        tid = str(tw.get("id") or "")
        out.append(
            RawNews(
                source="x", source_name=f"@{user}", title=text[:500], body=text,
                url=f"https://x.com/{user}/status/{tid}" if tid else None,
                published_at=min(_parse_date(tw.get("created_at")) or now, now), external_id=tid or None,
            )
        )  # fmt: skip
    return out
