"""Binance USDT-M Futures WebSocket streams with automatic reconnection.

- MarketStream: combined public streams (klines, mark price, ...).
- UserDataStream: private account/order updates via listenKey (keepalive every 30 min).

Binance closes connections after 24h, so we proactively reconnect before that, and on any error we
reconnect with exponential backoff (1s, 2s, 4s, ... up to `max_backoff`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from app.core.config import settings
from app.core.metrics import WS_CONNECTED, WS_RECONNECTS, safe_inc
from app.services.binance_client import BinanceFuturesClient

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
MAX_CONNECTION_AGE = 23 * 3600
LISTEN_KEY_KEEPALIVE = 30 * 60


def _set_gauge(label: str, value: float) -> None:
    try:
        WS_CONNECTED.labels(stream=label).set(value)
    except Exception:  # pragma: no cover
        pass


def ws_base_url(testnet: bool) -> str:
    return (settings.BINANCE_TESTNET_WS_URL if testnet else settings.BINANCE_WS_URL).rstrip("/")


class BinanceStream:
    def __init__(
        self,
        handler: Handler,
        *,
        name: str = "stream",
        max_backoff: float = 60.0,
        initial_backoff: float = 1.0,
        max_age: float = MAX_CONNECTION_AGE,
    ) -> None:
        self.handler = handler
        self.name = name
        self.max_backoff = max_backoff
        self.initial_backoff = initial_backoff
        self.max_age = max_age
        self.connections = 0
        self.messages = 0
        self._stop = asyncio.Event()
        self.down_since: float | None = time.monotonic()
        self.down_alerted = False
        self.down_alert_seconds = float(settings.ALERT_WS_DISCONNECT_SECONDS)
        # workspace_id/account_id attached to WS alerts (empty for shared market streams = global alert)
        self.alert_context: dict = {}

    @property
    def metric_label(self) -> str:
        return self.name

    async def on_down(self, seconds: float) -> None:
        """Hook: the stream has been disconnected for longer than `down_alert_seconds`."""
        logger.warning("ws %s disconnected for %.0fs", self.name, seconds)
        from app.services.alerts.service import emit_alert

        emit_alert(
            "ws_disconnected", dedup=f"ws:{self.name}", stream=self.name, seconds=round(seconds), **self.alert_context
        )

    async def on_recovered(self, seconds: float) -> None:
        """Hook: the stream reconnected after an `on_down` notification."""
        logger.info("ws %s recovered after %.0fs", self.name, seconds)
        from app.services.alerts.service import emit_alert

        emit_alert(
            "ws_disconnected",
            severity="info",
            dedup=f"ws-up:{self.name}",
            stream=self.name,
            seconds=round(seconds),
            recovered=True,
            **self.alert_context,
        )

    async def _mark_connected(self) -> None:
        if self.connections > 1:
            safe_inc(WS_RECONNECTS, stream=self.metric_label)
        _set_gauge(self.metric_label, 1)
        if self.down_alerted and self.down_since is not None:
            await self._safe_hook(self.on_recovered(time.monotonic() - self.down_since))
        self.down_since = None
        self.down_alerted = False

    async def _mark_disconnected(self) -> None:
        _set_gauge(self.metric_label, 0)
        now = time.monotonic()
        if self.down_since is None:
            self.down_since = now
        elif not self.down_alerted and now - self.down_since >= self.down_alert_seconds:
            self.down_alerted = True
            await self._safe_hook(self.on_down(now - self.down_since))

    async def _safe_hook(self, coro) -> None:
        try:
            await coro
        except Exception:
            logger.exception("ws %s hook failed", self.name)

    idle_timeout: float | None = None  # seconds without any message before forcing a reconnect

    async def url(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError

    async def on_open(self, ws) -> None:
        """Hook called after each successful (re)connection."""

    async def on_close(self) -> None:
        """Hook called after a connection ends."""

    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        backoff = self.initial_backoff
        while not self._stop.is_set():
            try:
                url = await self.url()
                async with ws_connect(url, ping_interval=20, ping_timeout=20, open_timeout=10) as ws:
                    self.connections += 1
                    backoff = self.initial_backoff
                    await self._mark_connected()
                    opened = last_msg = time.monotonic()
                    logger.info("ws %s connected (%d)", self.name, self.connections)
                    await self.on_open(ws)
                    while not self._stop.is_set():
                        remaining = self.max_age - (time.monotonic() - opened)
                        if remaining <= 0:
                            logger.info("ws %s max age reached; reconnecting", self.name)
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 1.0))
                        except TimeoutError:
                            if self.idle_timeout and time.monotonic() - last_msg > self.idle_timeout:
                                logger.warning("ws %s silent for %.0fs; reconnecting", self.name, self.idle_timeout)
                                break
                            continue
                        last_msg = time.monotonic()
                        self.messages += 1
                        msg = json.loads(raw)
                        if await self._dispatch(msg) is False:
                            break
            except (OSError, WebSocketException, TimeoutError, json.JSONDecodeError) as exc:
                logger.warning("ws %s error: %s", self.name, exc)
            except Exception:
                logger.exception("ws %s unexpected error", self.name)
            finally:
                await self.on_close()
            if self._stop.is_set():
                break
            await self._mark_disconnected()
            await self._sleep(backoff)
            backoff = min(backoff * 2, self.max_backoff)

    async def _dispatch(self, msg: dict[str, Any]) -> bool | None:
        await self.handler(msg)
        return None


class MarketStream(BinanceStream):
    """Combined stream, e.g. streams=["btcusdt@kline_1m", "btcusdt@markPrice@1s"]."""

    # Public market streams push data every second; a silent socket is a half-open connection
    # (observed behind proxies) that keepalive pings did not tear down, so reconnect.
    idle_timeout: float | None = 60.0

    def __init__(
        self, streams: list[str], handler: Handler, *, testnet: bool = True, base_url: str | None = None, **kw
    ):
        super().__init__(handler, name=kw.pop("name", "market"), **kw)
        self.streams = streams
        self.base_url = base_url or ws_base_url(testnet)

    @staticmethod
    def _stream_name(stream: str) -> str:
        # Only the symbol is case-insensitive (lowercase); stream types such as "!miniTicker@arr" or
        # "markPrice@1s" are case-sensitive and silently deliver nothing when lowercased.
        if stream.startswith("!"):
            return stream
        symbol, sep, rest = stream.partition("@")
        return f"{symbol.lower()}{sep}{rest}"

    async def url(self) -> str:
        return f"{self.base_url}/stream?streams={'/'.join(self._stream_name(s) for s in self.streams)}"

    async def _dispatch(self, msg: dict[str, Any]) -> bool | None:
        await self.handler(msg.get("data", msg))
        return None


class UserDataStream(BinanceStream):
    """Private stream for ORDER_TRADE_UPDATE / ACCOUNT_UPDATE events."""

    def __init__(
        self,
        client: BinanceFuturesClient,
        handler: Handler,
        *,
        base_url: str | None = None,
        keepalive_seconds: float = LISTEN_KEY_KEEPALIVE,
        **kw,
    ):
        super().__init__(handler, name=kw.pop("name", "user-data"), **kw)
        self.client = client
        self.base_url = base_url or ws_base_url(client.testnet)
        self.keepalive_seconds = keepalive_seconds
        self.listen_key: str | None = None
        self._keepalive_task: asyncio.Task | None = None

    async def url(self) -> str:
        self.listen_key = await self.client.create_listen_key()
        return f"{self.base_url}/ws/{self.listen_key}"

    async def on_open(self, ws) -> None:
        self._keepalive_task = asyncio.create_task(self._keepalive())

    async def on_close(self) -> None:
        if self._keepalive_task:
            self._keepalive_task.cancel()
            self._keepalive_task = None

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(self.keepalive_seconds)
            try:
                await self.client.keepalive_listen_key()
            except Exception as exc:  # next reconnect creates a new key
                logger.warning("listenKey keepalive failed: %s", exc)

    async def _dispatch(self, msg: dict[str, Any]) -> bool | None:
        if msg.get("e") == "listenKeyExpired":
            logger.info("listenKey expired; reconnecting")
            return False
        await self.handler(msg)
        return None
