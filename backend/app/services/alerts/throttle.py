"""Alert de-duplication/throttling: one alert per dedup key per window (Redis SET NX EX, memory fallback)."""

from __future__ import annotations

import asyncio
import hashlib
import time

from app.core.redis import get_redis

_memory: dict[str, float] = {}
PREFIX = "alerts:throttle:"


def _key(dedup_key: str) -> str:
    return PREFIX + hashlib.sha256(dedup_key.encode()).hexdigest()[:32]


async def acquire(dedup_key: str, minutes: int) -> bool:
    """True when the alert may be sent (first occurrence inside the window)."""
    if minutes <= 0:
        return True
    ttl = int(minutes * 60)
    key = _key(dedup_key)
    try:
        ok = await asyncio.wait_for(get_redis().set(key, "1", nx=True, ex=ttl), timeout=1.5)
        return bool(ok)
    except Exception:
        # Redis unavailable: best-effort per-process throttle so we never spam nor block.
        now = time.monotonic()
        for k, exp in list(_memory.items()):
            if exp <= now:
                _memory.pop(k, None)
        if key in _memory:
            return False
        _memory[key] = now + ttl
        return True


def reset_memory() -> None:
    _memory.clear()
