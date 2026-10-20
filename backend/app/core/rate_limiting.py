"""Simple fixed-window rate limiter backed by Redis (placeholder)."""
from fastapi import HTTPException, Request, status

from app.core.config import settings
from app.core.redis import get_redis


async def rate_limit(request: Request) -> None:
    """FastAPI dependency limiting requests per client IP per minute."""
    client = request.client.host if request.client else "unknown"
    key = f"rl:{client}:{request.url.path}"
    redis = get_redis()
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 60)
    if count > settings.RATE_LIMIT_PER_MINUTE:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="Muitas requisições")
