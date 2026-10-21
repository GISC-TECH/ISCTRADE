"""Bounded HTTP fetches for third-party content (news feeds, calendar, LLM).

httpx timeouts are per network operation: a server that drips one byte every few seconds never trips them, and
nothing caps the body size (a huge or gzip-bomb feed would be decompressed into memory). `fetch_limited` adds a
total deadline and a cap on the *decoded* body, then returns a regular `httpx.Response`.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx


class ResponseTooLarge(httpx.HTTPError):
    def __init__(self, url: str, limit: int) -> None:
        super().__init__(f"response from {httpx.URL(url).host} exceeds {limit} bytes")


async def _read(client: httpx.AsyncClient, method: str, url: str, max_bytes: int, kw: dict[str, Any]) -> httpx.Response:
    async with client.stream(method, url, **kw) as resp:
        declared = resp.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise ResponseTooLarge(url, max_bytes)
        chunks, size = [], 0
        async for chunk in resp.aiter_bytes():
            size += len(chunk)
            if size > max_bytes:
                raise ResponseTooLarge(url, max_bytes)
            chunks.append(chunk)
        headers = [
            (k, v) for k, v in resp.headers.multi_items() if k.lower() not in ("content-encoding", "content-length")
        ]
        return httpx.Response(resp.status_code, headers=headers, content=b"".join(chunks), request=resp.request)


async def fetch_limited(
    client: httpx.AsyncClient, method: str, url: str, *, max_bytes: int, deadline: float, **kw: Any
) -> httpx.Response:
    """`client.request(...)` with a total deadline (seconds) and a maximum decoded body size."""
    try:
        return await asyncio.wait_for(_read(client, method, url, max_bytes, kw), timeout=deadline)
    except TimeoutError:
        raise httpx.TimeoutException(f"{httpx.URL(url).host}: no complete response within {deadline}s") from None
