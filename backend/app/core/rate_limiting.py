"""Fixed-window rate limiter backed by Redis, with an in-memory fallback when Redis is unavailable."""

from __future__ import annotations

import ipaddress
import logging
import time
from collections import defaultdict
from functools import lru_cache

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


async def peek(key: str, window: int = 60) -> int:
    """Current count for `key` without incrementing it."""
    try:
        value = await get_redis().get(key)
        return int(value or 0)
    except Exception:  # pragma: no cover - depends on infra
        count, start = _memory.get(key, (0, 0.0))
        return count if time.monotonic() - start < window else 0


@lru_cache(maxsize=8)
def _trusted_networks(proxies: tuple[str, ...]) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets = []
    for p in proxies:
        try:
            nets.append(ipaddress.ip_network(p.strip(), strict=False))
        except ValueError:
            logger.warning("ignoring invalid TRUSTED_PROXIES entry %r", p)
    return tuple(nets)


def _is_trusted(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in _trusted_networks(tuple(settings.TRUSTED_PROXIES)))


def client_ip(request: Request) -> str:
    """Client address for rate limiting/audit.

    X-Forwarded-For is only honoured when the direct peer is a trusted proxy, and then the right-most
    untrusted hop is used (the left-most entries are client-controlled and trivially spoofed).
    """
    peer = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if not forwarded or not _is_trusted(peer):
        return peer
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _is_trusted(hop):
            return hop
    return hops[0] if hops else peer


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
