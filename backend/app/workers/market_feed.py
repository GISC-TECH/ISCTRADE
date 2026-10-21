"""Public market data feed for the real-time panel (runs inside the streams worker).

One combined Binance WebSocket per network with all-symbol mini tickers and mark price/funding
(1 update/s each) plus the kline streams currently requested by connected browsers (registered in Redis
by the API WebSocket, with expiry). Messages are published to ``rt:market:<net>``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.config import settings
from app.services import prices, realtime
from app.services.binance_ws import MarketStream

logger = logging.getLogger("streams.market_feed")

BASE_STREAMS = ("!miniTicker@arr", "!markPrice@arr@1s")
MAX_KLINE_STREAMS = 150  # Binance allows up to 200 streams per connection
Publisher = Callable[[str, dict[str, Any]], Awaitable[bool]]


def _f(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ticker_rows(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for t in data:
        o, c = _f(t.get("o")), _f(t.get("c"))
        pct = round((c - o) / o * 100, 3) if o and c is not None else None
        rows.append({"s": t.get("s"), "c": c, "o": o, "h": _f(t.get("h")), "l": _f(t.get("l")),
                     "q": _f(t.get("q")), "P": pct})  # fmt: skip
    return rows


def mark_rows(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"s": m.get("s"), "p": _f(m.get("p")), "r": _f(m.get("r")), "T": m.get("T")} for m in data]


def kline_row(data: dict[str, Any]) -> dict[str, Any]:
    k = data.get("k") or {}
    return {
        "s": k.get("s") or data.get("s"), "i": k.get("i"), "t": k.get("t"), "T": k.get("T"), "o": _f(k.get("o")),
        "h": _f(k.get("h")), "l": _f(k.get("l")), "c": _f(k.get("c")), "v": _f(k.get("v")), "x": bool(k.get("x")),
    }  # fmt: skip


class MarketFeed:
    def __init__(
        self,
        testnet: bool,
        publisher: Publisher | None = None,
        stream_factory: Callable[..., MarketStream] = MarketStream,
        cache_marks: bool = True,
    ) -> None:
        self.cache_marks = cache_marks  # feeds the mark price cache used by paper SL/TP/limit checks
        self.testnet = testnet
        self.channel = realtime.market_channel(testnet)
        self.publish = publisher or realtime.publish
        self.stream_factory = stream_factory
        self.streams: tuple[str, ...] = ()
        self.stream: MarketStream | None = None
        self.task: asyncio.Task | None = None
        self._last_kline: dict[str, float] = {}

    async def handle(self, data: Any) -> None:
        if isinstance(data, list):
            if not data or not isinstance(data[0], dict):
                return
            kind = data[0].get("e")
            if kind == "24hrMiniTicker":
                await self.publish(self.channel, realtime.message("tickers", ticker_rows(data)))
            elif kind == "markPriceUpdate":
                rows = mark_rows(data)
                await self.publish(self.channel, realtime.message("mark", rows))
                if self.cache_marks:
                    await prices.cache_prices({r["s"]: r["p"] for r in rows if r["s"] and r["p"]}, self.testnet)
            return
        if isinstance(data, dict) and data.get("e") == "kline":
            row = kline_row(data)
            key = f"{row['s']}:{row['i']}"
            now = time.monotonic()
            if not row["x"] and now - self._last_kline.get(key, 0) < settings.REALTIME_PRICE_THROTTLE_MS / 1000:
                return
            self._last_kline[key] = now
            await self.publish(self.channel, realtime.message("kline", row))

    async def target(self) -> tuple[str, ...]:
        try:
            klines = sorted(await realtime.wanted_klines(self.testnet))[:MAX_KLINE_STREAMS]
        except Exception as exc:  # Redis down: keep the current subscription set
            logger.debug("kline registry unavailable: %s", type(exc).__name__)
            return self.streams or BASE_STREAMS
        return (*BASE_STREAMS, *klines)

    async def sync(self) -> bool:
        """(Re)start the combined stream when the wanted set changed. Returns True when restarted."""
        target = await self.target()
        if target == self.streams and self.task is not None and not self.task.done():
            return False
        self.stop()
        self.streams = target
        self.stream = self.stream_factory(
            list(target), self.handle, testnet=self.testnet, name=f"panel-{'testnet' if self.testnet else 'mainnet'}"
        )
        self.task = asyncio.create_task(self.stream.run())
        return True

    def stop(self) -> None:
        if self.stream is not None:
            self.stream.stop()
        if self.task is not None:
            self.task.cancel()
        self.stream, self.task = None, None
