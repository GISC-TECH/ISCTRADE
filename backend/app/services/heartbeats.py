"""Heartbeats of the background processes (beat, Celery workers, bot ticks, streams).

Each process records "I am alive" in two places:
- a Redis key holding the Unix timestamp (read by /health/ready and the API's /metrics), and
- for beat/streams, a local file whose mtime is checked by the Docker healthcheck (``python -m app.healthcheck``),
  so the container health never depends on the network.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from app.core.config import settings

logger = logging.getLogger(__name__)

BEAT_KEY = "heartbeat:beat"
BOT_TICK_KEY = "heartbeat:bot_tick"
STREAMS_STATUS_KEY = "streams:status"
WORKER_QUEUES = ("trading", "default")


def worker_key(queue: str) -> str:
    return f"heartbeat:worker:{queue}"


# ---------------------------------------------------------------- files
def touch_file(path: str | os.PathLike[str]) -> None:
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(time.time()))
    except OSError:  # pragma: no cover - best effort
        logger.debug("could not touch heartbeat file %s", path, exc_info=True)


def file_age(path: str | os.PathLike[str], now: float | None = None) -> float | None:
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    return max(0.0, (now or time.time()) - mtime)


# ---------------------------------------------------------------- Redis (sync: beat, Celery task wrappers)
_sync_client = None
_sync_pid: int | None = None


def _sync_redis():
    global _sync_client, _sync_pid
    if _sync_client is None or _sync_pid != os.getpid():
        from redis import Redis

        _sync_client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=2, socket_timeout=2)
        _sync_pid = os.getpid()
    return _sync_client


def touch_sync(key: str, ttl_seconds: int, value: float | None = None) -> bool:
    """SET key=<now> EX ttl. Never raises (a heartbeat must not break the caller)."""
    try:
        _sync_redis().set(key, str(value if value is not None else time.time()), ex=max(int(ttl_seconds), 1))
        return True
    except Exception:
        logger.debug("heartbeat %s not written", key, exc_info=True)
        return False


# ---------------------------------------------------------------- Redis (async: API, streams)
def age_from_raw(raw: bytes | str | None, now: float | None = None) -> float | None:
    if raw is None:
        return None
    try:
        value = float(raw.decode() if isinstance(raw, bytes) else raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, round((now or time.time()) - value, 1))
