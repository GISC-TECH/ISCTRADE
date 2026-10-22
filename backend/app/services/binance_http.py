"""Shared HTTP plumbing for the Binance REST client: pooled connections and request-weight tracking.

* Connection pooling: one ``httpx.AsyncClient`` per (event loop, base URL), reused by every
  ``BinanceFuturesClient`` (bots, reconciler, scanner, prices, dashboard). Creating a client per call paid a
  TCP + TLS handshake on every tick/task. API keys are sent per request, so accounts can share the pool.
* Rate limits: Binance reports the IP-wide used weight in ``X-MBX-USED-WEIGHT-1M`` on every response.
  We track it per base URL; near the limit, non-order requests wait for the next minute window (or fail
  fast), and after a 429/418 every request fails locally until ``Retry-After`` instead of hammering the
  API (repeated 429s escalate to an IP ban, HTTP 418). Order placement/cancellation is never delayed by the
  soft limit (protective exits must go out), only by an active ban.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from dataclasses import dataclass

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=3.0, pool=5.0)
POOL_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=32, keepalive_expiry=60.0)

# Endpoints that must never be delayed by the soft weight guard (entries/exits, user stream keepalive).
PRIORITY_PATHS = frozenset(
    {
        "/fapi/v1/order", "/fapi/v1/allOpenOrders", "/fapi/v1/batchOrders", "/fapi/v1/listenKey",
        "/fapi/v1/algoOrder", "/fapi/v1/algoOpenOrders",  # protective STOP/TP (Algo Order API)
    }
)  # fmt: skip

_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, httpx.AsyncClient]] = weakref.WeakKeyDictionary()


def shared_client(base_url: str) -> httpx.AsyncClient:
    """Pooled client for `base_url` bound to the running event loop (clients cannot cross loops)."""
    loop = asyncio.get_running_loop()
    clients = _pools.get(loop)
    if clients is None:
        clients = _pools[loop] = {}
    client = clients.get(base_url)
    if client is None or client.is_closed:
        client = clients[base_url] = httpx.AsyncClient(base_url=base_url, timeout=DEFAULT_TIMEOUT, limits=POOL_LIMITS)
    return client


async def close_shared_clients() -> None:
    """Close the pooled clients of the running loop (worker shutdown / loop reset)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for client in (_pools.pop(loop, None) or {}).values():
        try:
            await client.aclose()
        except Exception:  # pragma: no cover
            pass


@dataclass
class WeightState:
    used_1m: int = 0
    window: int = -1  # minute (epoch // 60) of the last header seen
    banned_until: float = 0.0
    last_status: int = 0


class RateLimitGuard:
    """Per-base-URL view of the Binance IP request weight (shared by all clients of the process)."""

    def __init__(self) -> None:
        self._states: dict[str, WeightState] = {}

    def state(self, base_url: str) -> WeightState:
        st = self._states.get(base_url)
        if st is None:
            st = self._states[base_url] = WeightState()
        return st

    def reset(self) -> None:
        self._states.clear()

    @staticmethod
    def _limit() -> int:
        return max(int(settings.BINANCE_WEIGHT_LIMIT_1M), 1)

    def used(self, base_url: str, now: float | None = None) -> int:
        st = self.state(base_url)
        minute = int((now or time.time()) // 60)
        return st.used_1m if st.window == minute else 0

    def record(self, base_url: str, response: httpx.Response, now: float | None = None) -> None:
        now = now or time.time()
        st = self.state(base_url)
        st.last_status = response.status_code
        raw = response.headers.get("X-MBX-USED-WEIGHT-1M") or response.headers.get("X-MBX-USED-WEIGHT")
        if raw is not None:
            try:
                st.used_1m, st.window = int(raw), int(now // 60)
            except ValueError:
                pass
            _set_weight_gauge(base_url, st.used_1m)
        if response.status_code in (418, 429):
            retry = response.headers.get("Retry-After")
            try:
                wait = float(retry) if retry is not None else (120.0 if response.status_code == 418 else 60.0)
            except ValueError:
                wait = 60.0
            st.banned_until = max(st.banned_until, now + wait)
            _record_rate_limited(base_url, response.status_code)
            logger.warning(
                "binance rate limit hit (HTTP %s); pausing requests to %s for %.0fs",
                response.status_code, base_url, wait,
            )  # fmt: skip

    async def before_request(self, base_url: str, method: str, path: str) -> None:
        """Raise if banned; for non-priority requests wait (bounded) when the weight is near the limit."""
        from app.services.binance_client import BinanceAPIError

        now = time.time()
        st = self.state(base_url)
        if st.banned_until > now:
            raise BinanceAPIError(
                st.last_status or 429, -1003,
                f"rate limited by Binance; requests paused for {st.banned_until - now:.0f}s",
            )  # fmt: skip
        if path in PRIORITY_PATHS:
            return
        soft = self._limit() * float(settings.BINANCE_WEIGHT_SOFT_PCT) / 100
        if self.used(base_url, now) < soft:
            return
        wait = 60 - (now % 60) + 0.25  # the 1m weight window resets on the minute
        if wait > float(settings.BINANCE_WEIGHT_MAX_WAIT_SECONDS):
            raise BinanceAPIError(429, -1003, f"local weight guard: {self.used(base_url, now)} used in the last minute")
        logger.info("binance weight %s near the limit; waiting %.1fs", self.used(base_url, now), wait)
        await asyncio.sleep(wait)


def _record_rate_limited(base_url: str, status: int) -> None:
    try:
        from app.core.metrics import BINANCE_RATE_LIMITED

        BINANCE_RATE_LIMITED.labels(host=httpx.URL(base_url).host, status=str(status)).inc()
    except Exception:  # pragma: no cover - metrics must never break requests
        pass


def _set_weight_gauge(base_url: str, value: int) -> None:
    try:
        from app.core.metrics import BINANCE_USED_WEIGHT

        BINANCE_USED_WEIGHT.labels(host=httpx.URL(base_url).host).set(value)
    except Exception:  # pragma: no cover - metrics must never break requests
        pass


rate_limits = RateLimitGuard()
