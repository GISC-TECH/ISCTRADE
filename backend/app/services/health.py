"""Liveness/readiness checks shared by /health/ready, /api/v1/monitoring/status and /metrics."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis

STREAMS_HEARTBEAT_KEY = "streams:heartbeat"
CHECK_TIMEOUT = 2.0


async def _timed(coro) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT)
        return {"ok": True, "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
    except Exception as exc:
        return {"ok": False, "error": type(exc).__name__}


async def check_database() -> dict[str, Any]:
    async def run() -> None:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))

    return await _timed(run())


async def check_redis() -> dict[str, Any]:
    return await _timed(get_redis().ping())


async def check_broker() -> dict[str, Any]:
    url = settings.CELERY_BROKER_URL
    if not url.startswith(("redis://", "rediss://")):
        return {"ok": True, "skipped": "non-redis broker"}
    client = Redis.from_url(url, socket_connect_timeout=1, socket_timeout=2)
    try:
        return await _timed(client.ping())
    finally:
        await client.aclose()


async def streams_heartbeat_age() -> float | None:
    try:
        raw = await asyncio.wait_for(get_redis().get(STREAMS_HEARTBEAT_KEY), timeout=CHECK_TIMEOUT)
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return max(0.0, round(time.time() - float(raw), 1))
    except ValueError:
        return None


async def write_streams_heartbeat() -> None:
    try:
        await get_redis().set(STREAMS_HEARTBEAT_KEY, str(time.time()), ex=settings.STREAMS_HEARTBEAT_STALE_SECONDS * 4)
    except Exception:
        pass


async def check_streams() -> dict[str, Any]:
    age = await streams_heartbeat_age()
    if age is None:
        return {"ok": False, "state": "missing", "age_seconds": None}
    stale = age > settings.STREAMS_HEARTBEAT_STALE_SECONDS
    return {"ok": not stale, "state": "stale" if stale else "ok", "age_seconds": age}


CRITICAL = ("database", "redis", "broker")


async def readiness() -> dict[str, Any]:
    """database/redis/broker are critical (503 when down); streams is reported but only degrades."""
    database, redis, broker, streams = await asyncio.gather(
        check_database(), check_redis(), check_broker(), check_streams()
    )
    checks = {"database": database, "redis": redis, "broker": broker, "streams": streams}
    ready = all(checks[name]["ok"] for name in CRITICAL)
    status = "ready" if ready and streams["ok"] else ("degraded" if ready else "not_ready")
    return {"status": status, "ready": ready, "checks": checks}
