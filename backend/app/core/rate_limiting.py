"""Fixed-window rate limiter backed by Redis, with an in-memory fallback when Redis is unavailable."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

_memory: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))


def reset_memory_limiter() -> None:
    _memory.clear()


def _memory_hit(key: str, window: int) -> int:
    count, start = _memory[key]
    now = time.monotonic()
    if now - start >= window:
        count, start = 0, now
    count += 1
    _memory[key] = (count, start)
    return count


async def hit(key: str, window: int = 60) -> int:
    """Increment the counter for `key` and return the count in the current window."""
    try:
        redis = get_redis()
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window)
        return int(count)
    except Exception:  # pragma: no cover - depends on infra
        logger.debug("redis unavailable for rate limiting; using in-memory fallback")
        return _memory_hit(key, window)


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limiter(limit: int | None = None, window: int = 60, scope: str | None = None):
    """Dependency factory: `Depends(rate_limiter(10))` limits per client IP and route."""

    async def _dep(request: Request) -> None:
        max_hits = limit or settings.RATE_LIMIT_PER_MINUTE
        key = f"rl:{scope or request.url.path}:{client_ip(request)}"
        if await hit(key, window) > max_hits:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Muitas requisições. Tente novamente em instantes.",
                headers={"Retry-After": str(window)},
            )

    return _dep


rate_limit = rate_limiter()
