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
import uuid
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.core.database import session_scope
from app.core.logging import configure_logging
from app.core.metrics import start_metrics_server
from app.models import Account, AccountStatus, BotConfig, ExecutionMode
from app.services.accounts import client_for_account
from app.services.binance_ws import MarketStream, UserDataStream
from app.services.health import write_streams_heartbeat
from app.services.prices import cache_price
from app.services.reconciler import Reconciler
from app.strategy.state_machine import ACTIVE_STATES

logger = logging.getLogger("streams")
REFRESH_SECONDS = 60
HEARTBEAT_SECONDS = 15


class StreamSupervisor:
    def __init__(self) -> None:
        self.user_streams: dict[uuid.UUID, tuple[UserDataStream, asyncio.Task]] = {}
        self.market: dict[bool, tuple[MarketStream, asyncio.Task, frozenset[str]]] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        heartbeat = asyncio.create_task(self._heartbeat())
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
        await self.shutdown()

    async def _heartbeat(self) -> None:
        """Liveness signal read by /health/ready and the btp_streams_heartbeat_age_seconds gauge."""
        while not self._stop.is_set():
            await write_streams_heartbeat()
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
                self.user_streams[acc_id] = self._start_user_stream(account)

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
                stream = MarketStream(
                    [f"{s.lower()}@markPrice@1s" for s in sorted(syms)],
                    self._price_handler(testnet),
                    testnet=testnet,
                    name=f"market-{'testnet' if testnet else 'mainnet'}",
                )
                self.market[testnet] = (stream, asyncio.create_task(stream.run()), key)

    def _price_handler(self, testnet: bool):
        async def handle(data: dict[str, Any]) -> None:
            if data.get("e") == "markPriceUpdate":
                await cache_price(data["s"], data["p"], testnet)

        return handle

    def _start_user_stream(self, account: Account) -> tuple[UserDataStream, asyncio.Task]:
        client = client_for_account(account)
        account_id = account.id

        async def handle(event: dict[str, Any]) -> None:
            async with session_scope() as db:
                acc = await db.get(Account, account_id)
                if acc is None:
                    return
                await Reconciler(db, acc, client).handle_event(event)
                await db.commit()

        stream = UserDataStream(client, handle, name=f"user-{account_id}")
        return stream, asyncio.create_task(stream.run())

    async def shutdown(self) -> None:
        for stream, task in self.user_streams.values():
            stream.stop()
            task.cancel()
            await stream.client.close()
        for stream, task, _ in self.market.values():
            stream.stop()
            task.cancel()


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
