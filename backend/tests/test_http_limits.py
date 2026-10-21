"""Size cap and total deadline for third-party fetches (news/calendar/LLM)."""

import asyncio
import gzip

import httpx
import pytest

from app.core.config import settings
from app.core.http_limits import ResponseTooLarge, fetch_limited
from app.services.news import sources


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_small_response_is_returned_intact() -> None:
    async with _client(lambda r: httpx.Response(200, json={"ok": True})) as c:
        resp = await fetch_limited(c, "GET", "https://feed.example/x", max_bytes=1000, deadline=5)
    assert resp.status_code == 200 and resp.json() == {"ok": True}
    resp.raise_for_status()


async def test_declared_length_over_limit_rejected() -> None:
    async with _client(lambda r: httpx.Response(200, content=b"x" * 2000)) as c:
        with pytest.raises(ResponseTooLarge):
            await fetch_limited(c, "GET", "https://feed.example/x", max_bytes=1000, deadline=5)


async def test_streamed_body_over_limit_rejected_without_content_length() -> None:
    async def body():
        for _ in range(100):
            yield b"y" * 100

    async with _client(lambda r: httpx.Response(200, content=body())) as c:
        with pytest.raises(ResponseTooLarge):
            await fetch_limited(c, "GET", "https://feed.example/x", max_bytes=1000, deadline=5)


async def test_gzip_bomb_limited_on_decoded_size() -> None:
    bomb = gzip.compress(b"\0" * 1_000_000)  # ~1 KB on the wire, 1 MB decoded
    handler = lambda r: httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"})  # noqa: E731
    async with _client(handler) as c:
        with pytest.raises(ResponseTooLarge):
            await fetch_limited(c, "GET", "https://feed.example/x", max_bytes=100_000, deadline=5)


async def test_slow_drip_hits_total_deadline() -> None:
    async def drip():
        for _ in range(50):
            await asyncio.sleep(0.05)
            yield b"<"

    async with _client(lambda r: httpx.Response(200, content=drip())) as c:
        with pytest.raises(httpx.TimeoutException):
            await fetch_limited(c, "GET", "https://feed.example/x", max_bytes=10_000, deadline=0.3)


async def test_rss_fetch_uses_limits(monkeypatch) -> None:
    monkeypatch.setattr(settings, "NEWS_MAX_RESPONSE_BYTES", 500)
    async with _client(lambda r: httpx.Response(200, content=b"<rss>" + b" " * 1000 + b"</rss>")) as c:
        with pytest.raises(ResponseTooLarge):
            await sources.fetch_rss(c, "https://feed.example/rss")
