"""Shared async Redis client (one per event loop)."""

import asyncio

from redis.asyncio import Redis

from app.core.config import settings

_clients: dict[int, Redis] = {}


def get_redis() -> Redis:
    """Return a Redis client bound to the running event loop."""
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        loop_id = 0
    client = _clients.get(loop_id)
    if client is None:
        client = Redis.from_url(settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
        _clients[loop_id] = client
    return client


async def close_redis() -> None:
    try:
        loop_id = id(asyncio.get_running_loop())
    except RuntimeError:
        return
    client = _clients.pop(loop_id, None)
    if client is not None:
        await client.aclose()
