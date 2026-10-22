"""Long-running WebSocket worker.

- One UserDataStream per RUNNING live account (only when LIVE_TRADING_ENABLED): order/fill/position
  events are applied through the Reconciler in real time.
- One MarketStream with mark prices for every symbol of active bots, cached in Redis.

Run with:  python -m app.workers.streams
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time
import uuid
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import configure_logging
from app.core.metrics import WS_CONNECTED, WS_LAST_MESSAGE_AGE, start_metrics_server
from app.exchanges.base import ExchangeStream, UnsupportedFeatureError
from app.exchanges.registry import get_adapter_class
from app.models import Account, AccountStatus, BotConfig, ExecutionMode
from app.services import realtime
from app.services.accounts import client_for_account
from app.services.health import write_streams_heartbeat
from app.services.heartbeats import touch_file
from app.services.prices import cache_prices
from app.services.reconciler import Reconciler
from app.strategy.state_machine import ACTIVE_STATES
from app.workers.market_feed import MarketFeed

logger = logging.getLogger("streams")
REFRESH_SECONDS = 60
HEARTBEAT_SECONDS = 15
FEED_SYNC_SECONDS = 5
PRICE_FLUSH_SECONDS = 0.5  # mark prices are buffered and written to Redis in one pipeline per flush


def user_event_summary(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Non-sensitive summary of a user-data event for the real-time panel."""
    kind = event.get("e")
    if kind == "ORDER_TRADE_UPDATE":
        o = event.get("o") or {}
        return "order_update", {
            "symbol": o.get("s"), "side": o.get("S"), "type": o.get("o"), "status": o.get("X"), "exec": o.get("x"),
            "last_qty": o.get("l"), "last_price": o.get("L"), "avg_price": o.get("ap"), "realized_pnl": o.get("rp"),
            "reduce_only": o.get("R"), "client_order_id": o.get("c"),
        }  # fmt: skip
    if kind == "ACCOUNT_UPDATE":
        a = event.get("a") or {}
        positions = [
            {"symbol": p.get("s"), "amount": p.get("pa"), "entry_price": p.get("ep"), "unrealized_pnl": p.get("up")}
            for p in a.get("P") or []
        ]
        balances = [{"asset": b.get("a"), "wallet": b.get("wb")} for b in a.get("B") or [] if b.get("a") == "USDT"]
        return "account_update", {"reason": a.get("m"), "positions": positions, "balances": balances}
    return None


class StreamSupervisor:
    def __init__(self) -> None:
        self.user_streams: dict[uuid.UUID, tuple[ExchangeStream, asyncio.Task]] = {}
        self.market: dict[bool, tuple[ExchangeStream, asyncio.Task, frozenset[str]]] = {}
        self.feeds: list[MarketFeed] = []
        self._stop = asyncio.Event()
        self._prices: dict[bool, dict[str, str]] = {True: {}, False: {}}
        self._reported: set[tuple[str, str]] = set()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        heartbeat = asyncio.create_task(self._heartbeat())
        price_flusher = asyncio.create_task(self._price_flush_loop())
        feeds = (
            asyncio.create_task(self._feed_loop())
            if settings.REALTIME_ENABLED and settings.REALTIME_MARKET_FEED
            else None
        )
        while not self._stop.is_set():
            try:
                await self.refresh()
            except Exception:
                logger.exception("stream refresh failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=REFRESH_SECONDS)
            except TimeoutError:
                pass
        heartbeat.cancel()
        price_flusher.cancel()
        await self.flush_prices()
        if feeds is not None:
            feeds.cancel()
        await self.shutdown()

    async def _feed_loop(self) -> None:
        """Public market data for the real-time panel (tickers, mark/funding, requested klines)."""
        self.feeds = [MarketFeed(testnet=False), MarketFeed(testnet=True)]
        while not self._stop.is_set():
            for feed in self.feeds:
                try:
                    await feed.sync()
                except Exception:
                    logger.exception("market feed sync failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=FEED_SYNC_SECONDS)
            except TimeoutError:
                pass

    def status(self, now: float | None = None) -> dict[str, Any]:
        """Real state of every WebSocket (read by /health/ready through Redis); also updates the age gauges."""
        now = now or time.time()
        items: list[dict[str, Any]] = []

        def add(stream: Any, kind: str) -> None:
            if stream is None:
                return
            last = getattr(stream, "last_message_at", None)
            name = str(getattr(stream, "name", kind))
            items.append({
                "name": name, "kind": kind, "connected": bool(getattr(stream, "connected", False)),
                "last_message_at": last, "connections": int(getattr(stream, "connections", 0) or 0),
            })  # fmt: skip
            try:
                WS_LAST_MESSAGE_AGE.labels(stream=name, kind=kind).set(-1 if last is None else max(0.0, now - last))
            except Exception:  # pragma: no cover - metrics never break the worker
                pass

        for stream, _task in self.user_streams.values():
            add(stream, "user")
        for stream, _task, _symbols in self.market.values():
            add(stream, "market")
        for feed in self.feeds:
            add(getattr(feed, "stream", None), "feed")
        # Streams that were stopped (account paused, symbols changed) must not leave stale series behind,
        # otherwise "disconnected"/"stale data" alerts would fire forever.
        current = {(i["name"], i["kind"]) for i in items}
        for name, kind in self._reported - current:
            for gauge, labels in ((WS_LAST_MESSAGE_AGE, (name, kind)), (WS_CONNECTED, (name,))):
                try:
                    gauge.remove(*labels)
                except KeyError:
                    pass
        self._reported = current
        return {"ts": now, "streams": items}

    async def _heartbeat(self) -> None:
        """Liveness signal: Redis (/health/ready, btp_streams_heartbeat_age_seconds) + local file (Docker)."""
        while not self._stop.is_set():
            touch_file(settings.STREAMS_HEARTBEAT_FILE)
            await write_streams_heartbeat(self.status())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                pass

    async def refresh(self) -> None:
        async with session_scope() as db:
            live_accounts: list[Account] = []
            if settings.LIVE_TRADING_ENABLED:
                live_accounts = list(
                    await db.scalars(
                        select(Account).where(
                            Account.execution_mode == ExecutionMode.LIVE, Account.status == AccountStatus.RUNNING
                        )
                    )
                )
            rows = await db.execute(
                select(BotConfig.symbol, Account.is_testnet)
                .join(Account, Account.id == BotConfig.account_id)
                .where(BotConfig.state.in_(ACTIVE_STATES))
            )
            symbols: dict[bool, set[str]] = {True: set(), False: set()}
            for symbol, testnet in rows:
                symbols[bool(testnet)].add(symbol)

        wanted = {a.id: a for a in live_accounts}
        for acc_id in list(self.user_streams):
            if acc_id not in wanted:
                stream, task = self.user_streams.pop(acc_id)
                stream.stop()
                task.cancel()
        for acc_id, account in wanted.items():
            if acc_id not in self.user_streams:
                started = self._start_user_stream(account)
                if started is not None:
                    self.user_streams[acc_id] = started

        for testnet, syms in symbols.items():
            current = self.market.get(testnet)
            key = frozenset(syms)
            if current and current[2] == key:
                continue
            if current:
                current[0].stop()
                current[1].cancel()
                self.market.pop(testnet)
            if syms:
                # Shared mark-price feed of the default exchange (Binance USDT-M).
                stream = get_adapter_class().market_stream(
                    [f"{s.lower()}@markPrice@1s" for s in sorted(syms)],
                    self._price_handler(testnet),
                    testnet=testnet,
                    name=f"market-{'testnet' if testnet else 'mainnet'}",
                )
                self.market[testnet] = (stream, asyncio.create_task(stream.run()), key)

    def _price_handler(self, testnet: bool):
        """Buffer the latest mark per symbol: the WS reader never waits for a Redis round trip per message."""
        buffer = self._prices[testnet]

        async def handle(data: dict[str, Any]) -> None:
            if data.get("e") == "markPriceUpdate":
                buffer[data["s"]] = data["p"]

        return handle

    async def flush_prices(self) -> None:
        for testnet, buffer in self._prices.items():
            if buffer:
                values = dict(buffer)  # copy + clear is atomic for the event loop (handlers share this dict)
                buffer.clear()
                await cache_prices(values, testnet)

    async def _price_flush_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.flush_prices()
            except Exception:  # pragma: no cover - cache is best effort
                logger.debug("price flush failed", exc_info=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=PRICE_FLUSH_SECONDS)
            except TimeoutError:
                pass

    def _start_user_stream(self, account: Account) -> tuple[ExchangeStream, asyncio.Task] | None:
        client = client_for_account(account)
        account_id = account.id
        workspace_id = account.workspace_id

        async def handle(event: dict[str, Any]) -> None:
            async with session_scope() as db:
                acc = await db.get(Account, account_id)
                if acc is None:
                    return
                await Reconciler(db, acc, client).handle_event(event)
                await db.commit()
            update = user_event_summary(event)
            if update is not None:
                await realtime.publish(
                    realtime.workspace_channel(workspace_id),
                    realtime.message(update[0], {"account_id": account_id, **update[1]}),
                )

        try:
            stream = client.user_data_stream(handle, name=f"user-{account_id}")
        except UnsupportedFeatureError:
            logger.warning("exchange %s has no user-data stream; skipping account %s", client.exchange_id, account_id)
            asyncio.ensure_future(client.close())
            return None
        stream.alert_context = {"workspace_id": account.workspace_id, "account_id": account_id}
        return stream, asyncio.create_task(stream.run())

    async def shutdown(self) -> None:
        for stream, task in self.user_streams.values():
            stream.stop()
            task.cancel()
            await stream.client.close()
        for stream, task, _ in self.market.values():
            stream.stop()
            task.cancel()
        for feed in self.feeds:
            feed.stop()


async def main() -> None:
    configure_logging()
    start_metrics_server(settings.METRICS_PORT)
    sup = StreamSupervisor()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, sup.stop)
    await sup.run()


if __name__ == "__main__":
    asyncio.run(main())
