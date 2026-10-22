"""Liveness/readiness checks shared by /health/ready, /api/v1/monitoring/status and /metrics.

Critical (503 when failing): database, redis, broker.
Reported and "degraded" when failing (503 only with HEALTH_READY_STRICT=true):
- beat: heartbeat written by the beat scheduler itself (app.tasks.beat.HeartbeatScheduler);
- worker_trading / worker_default: heartbeat written by ``ops.heartbeat``, scheduled by beat on each queue
  (proves beat -> broker -> worker end to end);
- streams: process heartbeat + real state of each WebSocket (connected, age of the last message);
- bot_ticks: when bots are due, the age of the last completed tick.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis import get_redis
from app.services.heartbeats import (
    BEAT_KEY,
    BOT_TICK_KEY,
    STREAMS_STATUS_KEY,
    WORKER_QUEUES,
    age_from_raw,
    worker_key,
)

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


async def _get(key: str) -> str | None:
    try:
        return await asyncio.wait_for(get_redis().get(key), timeout=CHECK_TIMEOUT)
    except Exception:
        return None


async def key_age(key: str) -> float | None:
    """Seconds since the heartbeat stored in `key` (None = missing/unreadable)."""
    return age_from_raw(await _get(key))


async def streams_heartbeat_age() -> float | None:
    return await key_age(STREAMS_HEARTBEAT_KEY)


async def write_streams_heartbeat(status: dict[str, Any] | None = None) -> None:
    ttl = settings.STREAMS_HEARTBEAT_STALE_SECONDS * 4
    try:
        r = get_redis()
        await r.set(STREAMS_HEARTBEAT_KEY, str(time.time()), ex=ttl)
        if status is not None:
            await r.set(STREAMS_STATUS_KEY, json.dumps(status), ex=ttl)
    except Exception:
        pass


async def streams_status() -> dict[str, Any] | None:
    raw = await _get(STREAMS_STATUS_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def evaluate_streams(age: float | None, status: dict[str, Any] | None, now: float | None = None) -> dict[str, Any]:
    """Process heartbeat + each WebSocket: connected, and (market streams) a message in the last N seconds."""
    if age is None:
        return {"ok": False, "state": "missing", "age_seconds": None, "streams": []}
    if age > settings.STREAMS_HEARTBEAT_STALE_SECONDS:
        return {"ok": False, "state": "stale", "age_seconds": age, "streams": []}
    now = now or time.time()
    streams: list[dict[str, Any]] = []
    problems: list[str] = []
    for s in (status or {}).get("streams", []):
        last = s.get("last_message_at")
        msg_age = None if last is None else max(0.0, round(now - float(last), 1))
        item = {
            "name": s.get("name"),
            "kind": s.get("kind"),
            "connected": bool(s.get("connected")),
            "last_message_age_seconds": msg_age,
        }
        if not item["connected"]:
            problems.append(f"{item['name']}: desconectado")
        elif item["kind"] == "market" and (msg_age is None or msg_age > settings.STREAMS_MESSAGE_STALE_SECONDS):
            problems.append(f"{item['name']}: sem mensagens")  # mark price is pushed every second
        streams.append(item)
    result: dict[str, Any] = {"ok": not problems, "state": "ok" if not problems else "degraded",
                              "age_seconds": age, "streams": streams}  # fmt: skip
    if problems:
        result["problems"] = problems
    return result


async def check_streams() -> dict[str, Any]:
    age, status = await asyncio.gather(streams_heartbeat_age(), streams_status())
    return evaluate_streams(age, status)


def _freshness(age: float | None, stale_after: float) -> dict[str, Any]:
    if age is None:
        return {"ok": False, "state": "missing", "age_seconds": None}
    stale = age > stale_after
    return {"ok": not stale, "state": "stale" if stale else "ok", "age_seconds": age}


async def check_beat() -> dict[str, Any]:
    return _freshness(await key_age(BEAT_KEY), settings.BEAT_HEARTBEAT_STALE_SECONDS)


async def check_worker(queue: str) -> dict[str, Any]:
    return _freshness(await key_age(worker_key(queue)), settings.WORKER_HEARTBEAT_STALE_SECONDS)


async def due_bots() -> int:
    from sqlalchemy import func, select

    from app.models import Account, AccountStatus, BotConfig, BotState

    async def run() -> int:
        async with AsyncSessionLocal() as db:
            q = (
                select(func.count(BotConfig.id))
                .join(Account, Account.id == BotConfig.account_id)
                .where(
                    BotConfig.is_enabled.is_(True),
                    BotConfig.state.in_((BotState.STARTING, BotState.RUNNING)),
                    Account.status == AccountStatus.RUNNING,
                )
            )
            return int((await db.execute(q)).scalar_one())

    try:
        return await asyncio.wait_for(run(), timeout=CHECK_TIMEOUT)
    except Exception:
        return 0


async def check_bot_ticks() -> dict[str, Any]:
    bots = await due_bots()
    age = await key_age(BOT_TICK_KEY)
    if bots == 0:
        return {"ok": True, "state": "idle", "bots": 0, "age_seconds": age}
    stale_after = max(settings.BOT_TICK_STALE_SECONDS, settings.BOT_TICK_SECONDS * 3)
    return {**_freshness(age, stale_after), "bots": bots}


CRITICAL = ("database", "redis", "broker")


async def readiness() -> dict[str, Any]:
    """`ready` = critical deps OK; `status` = ready | degraded | not_ready (see module docstring)."""
    names = ["database", "redis", "broker", "beat", "worker_trading", "worker_default", "streams", "bot_ticks"]
    results = await asyncio.gather(
        check_database(),
        check_redis(),
        check_broker(),
        check_beat(),
        *(check_worker(q) for q in WORKER_QUEUES),
        check_streams(),
        check_bot_ticks(),
    )
    checks = dict(zip(names, results, strict=True))
    ready = all(checks[name]["ok"] for name in CRITICAL)
    healthy = all(c["ok"] for c in checks.values())
    status = "ready" if healthy else ("degraded" if ready else "not_ready")
    return {"status": status, "ready": ready, "checks": checks}


def http_status(result: dict[str, Any]) -> int:
    if not result["ready"]:
        return 503
    if settings.HEALTH_READY_STRICT and result["status"] != "ready":
        return 503
    return 200
