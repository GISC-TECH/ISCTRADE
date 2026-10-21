"""Real-time fan-out over Redis pub/sub.

Producers (API, Celery, streams worker) publish small JSON messages; the API WebSocket endpoint
(`/api/v1/ws`) subscribes and forwards them to authorised browsers.

Channels:
* ``rt:market:main`` / ``rt:market:test``: public market data (tickers, mark/funding, klines)
* ``rt:ws:<workspace_id>``: private workspace events (orders, fills, positions, bots, alerts, account)
* ``rt:global``: shared non-sensitive events (scanner, news, calendar, global kill switch)

Publishing never raises and never blocks trading: inside a DB transaction, :func:`queue_event` defers the
message until the transaction commits (dropped on rollback), like the alert pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.redaction import redact
from app.core.redis import get_redis

logger = logging.getLogger(__name__)

PENDING_KEY = "pending_realtime"
GLOBAL = "rt:global"
KLINE_WANTED_PREFIX = "rt:kline_wanted:"  # sorted set per network: member "btcusdt@kline_1m", score = expiry


def market_channel(testnet: bool) -> str:
    return f"rt:market:{'test' if testnet else 'main'}"


def workspace_channel(workspace_id: uuid.UUID | str) -> str:
    return f"rt:ws:{workspace_id}"


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def encode(message: dict[str, Any]) -> str:
    return json.dumps(message, default=_default, separators=(",", ":"))


def message(kind: str, data: dict[str, Any] | list[Any] | None = None, **extra: Any) -> dict[str, Any]:
    return {"type": kind, "ts": int(time.time() * 1000), "data": data if data is not None else {}, **extra}


class MemoryBroker:
    """In-process fallback used when Redis is unreachable (single API process, development and tests).

    Thread-safe: publishers may run in other threads/event loops (e.g. the after-commit publisher thread).
    """

    def __init__(self) -> None:
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue, frozenset[str]]] = []
        self._lock = threading.Lock()

    def subscribe(self, channels: set[str]) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subs.append((asyncio.get_running_loop(), queue, frozenset(channels)))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s[1] is not queue]

    def publish(self, channel: str, data: str) -> int:
        with self._lock:
            targets = [(loop, q) for loop, q, chans in self._subs if channel in chans]
        for loop, queue in targets:
            try:
                loop.call_soon_threadsafe(_put, queue, (channel, data))
            except RuntimeError:  # loop closed
                self.unsubscribe(queue)
        return len(targets)


def _put(queue: asyncio.Queue, item: tuple[str, str]) -> None:
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:  # slow consumer: drop (market data is superseded by the next update)
        pass


memory_broker = MemoryBroker()


async def publish(channel: str, msg: dict[str, Any]) -> bool:
    if not settings.REALTIME_ENABLED:
        return False
    data = encode(msg)
    try:
        await asyncio.wait_for(get_redis().publish(channel, data), timeout=0.5)
        return True
    except Exception as exc:
        logger.debug("realtime publish via redis failed: %s", type(exc).__name__)
        return memory_broker.publish(channel, data) > 0


def _publish_sync(items: list[tuple[str, dict[str, Any]]]) -> None:
    encoded = [(channel, encode(msg)) for channel, msg in items]
    try:
        from redis import Redis

        client = Redis.from_url(settings.REDIS_URL, socket_connect_timeout=0.5, socket_timeout=0.5)
        try:
            pipe = client.pipeline(transaction=False)
            for channel, data in encoded:
                pipe.publish(channel, data)
            pipe.execute()
        finally:
            client.close()
    except Exception as exc:
        logger.debug("realtime publish via redis failed: %s", type(exc).__name__)
        for channel, data in encoded:
            memory_broker.publish(channel, data)


def queue_event(db: AsyncSession, workspace_id: uuid.UUID | str | None, kind: str, data: dict[str, Any]) -> None:
    """Publish after the surrounding transaction commits (no-op on rollback)."""
    if not settings.REALTIME_ENABLED:
        return
    channel = workspace_channel(workspace_id) if workspace_id else GLOBAL
    try:
        db.sync_session.info.setdefault(PENDING_KEY, []).append((channel, message(kind, redact(data))))
    except Exception:  # pragma: no cover
        logger.exception("queue_event failed")


@sa_event.listens_for(Session, "after_commit")
def _after_commit(session: Session) -> None:
    pending = session.info.pop(PENDING_KEY, None)
    if pending:
        threading.Thread(target=_publish_sync, args=(pending,), daemon=True, name="realtime-publish").start()


@sa_event.listens_for(Session, "after_soft_rollback")
def _after_rollback(session: Session, previous_transaction: Any) -> None:
    if previous_transaction.parent is None:
        session.info.pop(PENDING_KEY, None)


# audit event -> realtime message type
AUDIT_TYPES = {
    "order.filled": "order",
    "order.submitted": "order",
    "order.rejected": "order",
    "order.canceled": "order",
    "position.opened": "position",
    "position.closed": "position",
    "position.stop_loss": "position",
    "position.take_profit": "position",
    "risk.kill_switch": "kill_switch",
    "admin.global_kill_switch": "kill_switch",
    "bot.error": "bot",
    "bot.event_pause": "bot",
    "bot.entry_filtered": "bot",
    "manual.order": "manual",
    "manual.close": "manual",
    "manual.close_all": "manual",
    "manual.leverage": "manual",
    "manual.cancel": "manual",
    "manual.order_blocked": "manual",
}


def from_audit(
    db: AsyncSession, event: str, workspace_id: uuid.UUID | None, account_id: uuid.UUID | None, data: dict
) -> None:
    kind = AUDIT_TYPES.get(event) or (event.split(".", 1)[0] if event.startswith("bot.") else None)
    if kind is None:
        return
    payload = {"event": event, "account_id": account_id, **data}
    if event == "admin.global_kill_switch":
        queue_event(db, None, kind, payload)
    elif workspace_id:
        queue_event(db, workspace_id, kind, payload)


# --------------------------------------------------------------------------- kline subscriptions


async def want_klines(testnet: bool, streams: list[str]) -> None:
    """Register (and renew) kline streams requested by connected clients; read by the streams worker."""
    if not streams:
        return
    expiry = time.time() + settings.REALTIME_KLINE_TTL_SECONDS
    try:
        await asyncio.wait_for(
            get_redis().zadd(KLINE_WANTED_PREFIX + ("test" if testnet else "main"), dict.fromkeys(streams, expiry)),
            timeout=0.5,
        )
    except Exception as exc:
        logger.debug("kline subscription registry unavailable: %s", type(exc).__name__)


async def wanted_klines(testnet: bool) -> set[str]:
    key = KLINE_WANTED_PREFIX + ("test" if testnet else "main")
    r = get_redis()
    now = time.time()
    await r.zremrangebyscore(key, 0, now)
    return set(await r.zrangebyscore(key, now, "+inf"))
