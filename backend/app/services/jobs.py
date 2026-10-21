"""Background job logic (framework-agnostic). Celery tasks in app.tasks are thin wrappers around these."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.core.metrics import BOT_TICKS, safe_inc
from app.core.redis import get_redis
from app.models import Account, AccountStatus, BotConfig, BotState, ExecutionMode
from app.models.base import utcnow
from app.services import realtime
from app.services.audit import log_event
from app.services.bot_runner import run_bot_tick
from app.services.equity import compute_equity, open_positions, refresh_paper_marks, take_snapshot
from app.services.market_data import BinanceMarketData, MarketData
from app.strategy.state_machine import BotStateMachine

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], Any]


@asynccontextmanager
async def redis_lock(key: str, ttl: int = 120):
    """Best-effort distributed lock. Yields True if acquired (or Redis is unavailable)."""
    token = uuid.uuid4().hex
    acquired = True
    redis = None
    try:
        redis = get_redis()
        acquired = bool(await redis.set(f"lock:{key}", token, nx=True, ex=ttl))
    except Exception:
        redis = None  # no Redis -> single-process mode
    try:
        yield acquired
    finally:
        if redis is not None and acquired:
            try:
                if await redis.get(f"lock:{key}") == token:
                    await redis.delete(f"lock:{key}")
            except Exception:
                pass


# --------------------------------------------------------------------------- bots
async def due_bot_ids(sessions: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    async with sessions() as db:
        rows = await db.scalars(
            select(BotConfig.id)
            .join(Account, Account.id == BotConfig.account_id)
            .where(
                BotConfig.is_enabled.is_(True),
                BotConfig.state.in_((BotState.STARTING, BotState.RUNNING)),
                Account.status == AccountStatus.RUNNING,
            )
        )
        return list(rows)


async def tick_bot(
    sessions: async_sessionmaker[AsyncSession],
    bot_id: uuid.UUID,
    market_factory: Callable[[Account], MarketData] | None = None,
) -> dict[str, Any]:
    async with redis_lock(f"bot:{bot_id}") as acquired:
        if not acquired:
            return {"bot_id": str(bot_id), "status": "skipped", "message": "locked"}
        async with sessions() as db:
            bot = await db.get(BotConfig, bot_id)
            if bot is None:
                return {"bot_id": str(bot_id), "status": "missing"}
            account = await db.get(Account, bot.account_id)
            market = (market_factory or (lambda a: BinanceMarketData(testnet=a.is_testnet)))(account)
            try:
                result = await run_bot_tick(db, bot, account, market)
            finally:
                close = getattr(market, "close", None)
                if close:
                    await close()
            safe_inc(BOT_TICKS, result=result.status)
            await realtime.publish(
                realtime.workspace_channel(bot.workspace_id),
                realtime.message("bot", {
                    "event": "bot.tick", "bot_id": bot.id, "name": bot.name, "symbol": bot.symbol,
                    "state": bot.state, "last_tick_at": bot.last_tick_at, "last_error": bot.last_error,
                    "result": result.status, "message": result.message, "filled": result.filled,
                    "rejected": result.rejected, "reasons": result.reasons[:10],
                    "auto": (bot.runtime or {}).get("auto"), "event_pause": (bot.runtime or {}).get("event_pause"),
                }),
            )  # fmt: skip
            return {"bot_id": str(bot_id), "status": result.status, "message": result.message,
                    "filled": result.filled, "rejected": result.rejected}  # fmt: skip


async def prepare_live_bot(db: AsyncSession, bot: BotConfig, account: Account) -> None:
    """Configure leverage and isolated margin on the exchange before the first live order."""
    if account.execution_mode != ExecutionMode.LIVE or not settings.LIVE_TRADING_ENABLED:
        return
    from app.services.accounts import client_for_account

    async with client_for_account(account) as client:
        from app.services.position_mode import ensure_one_way

        await ensure_one_way(client, account.id)
        await client.set_margin_type(bot.symbol, "ISOLATED")
        await client.set_leverage(bot.symbol, bot.leverage)


async def start_bot(sessions: async_sessionmaker[AsyncSession], bot_id: uuid.UUID) -> dict[str, Any]:
    async with sessions() as db:
        bot = await db.get(BotConfig, bot_id)
        if bot is None or bot.state != BotState.STARTING:
            return {"bot_id": str(bot_id), "status": "skipped"}
        account = await db.get(Account, bot.account_id)
        try:
            await prepare_live_bot(db, bot, account)
        except Exception as exc:
            bot.state = BotStateMachine(bot.state).fail()
            bot.last_error = str(exc)[:2000]
            await log_event(db, "bot.error", level="error", workspace_id=bot.workspace_id,
                            account_id=bot.account_id, bot_id=bot.id, error=str(exc)[:500])  # fmt: skip
            await db.commit()
            return {"bot_id": str(bot_id), "status": "error", "message": str(exc)}
        await db.commit()
    return {"bot_id": str(bot_id), "status": "prepared"}


# --------------------------------------------------------------------------- accounts
async def reconcile_account(sessions: async_sessionmaker[AsyncSession], account_id: uuid.UUID) -> dict[str, Any]:
    from app.services.accounts import client_for_account
    from app.services.reconciler import Reconciler

    async with sessions() as db:
        account = await db.get(Account, account_id)
        if account is None or account.execution_mode != ExecutionMode.LIVE:
            return {"account_id": str(account_id), "status": "skipped"}
        async with client_for_account(account) as client:
            summary = await Reconciler(db, account, client).run()
        await db.commit()
        return {"account_id": str(account_id), **summary}


async def live_account_ids(sessions: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    if not settings.LIVE_TRADING_ENABLED:
        return []
    async with sessions() as db:
        rows = await db.scalars(
            select(Account.id).where(
                Account.execution_mode == ExecutionMode.LIVE,
                Account.status.in_((AccountStatus.RUNNING, AccountStatus.PAUSED, AccountStatus.KILLED)),
            )
        )
        return list(rows)


async def snapshot_account(
    sessions: async_sessionmaker[AsyncSession],
    account_id: uuid.UUID,
    price_lookup: Callable[[str, bool], Any] | None = None,
) -> dict[str, Any]:
    from app.services.prices import get_price

    lookup = price_lookup or get_price
    async with sessions() as db:
        account = await db.get(Account, account_id)
        if account is None:
            return {"account_id": str(account_id), "status": "missing"}
        client = None
        if account.execution_mode == ExecutionMode.LIVE and settings.LIVE_TRADING_ENABLED:
            from app.services.accounts import client_for_account

            client = client_for_account(account)
        else:
            prices: dict[str, Decimal] = {}
            for pos in await open_positions(db, account):
                price = await lookup(pos.symbol, account.is_testnet)
                if price is not None:
                    prices[pos.symbol] = Decimal(price)
            await refresh_paper_marks(db, account, prices)
        try:
            snap = await take_snapshot(db, account, client)
        finally:
            if client is not None:
                await client.close()
        await db.commit()
        await realtime.publish(
            realtime.workspace_channel(account.workspace_id),
            realtime.message("account", {"account_id": account.id, "equity": snap.equity, "status": account.status}),
        )
        return {"account_id": str(account_id), "equity": str(snap.equity)}


async def snapshot_account_ids(sessions: async_sessionmaker[AsyncSession]) -> list[uuid.UUID]:
    async with sessions() as db:
        return list(await db.scalars(select(Account.id)))


async def daily_reset(sessions: async_sessionmaker[AsyncSession]) -> int:
    """00:00 UTC: re-baseline the daily-loss reference for every account."""
    now = utcnow()
    count = 0
    async with sessions() as db:
        for account in await db.scalars(select(Account)):
            if account.execution_mode == ExecutionMode.LIVE:
                equity = Decimal(account.last_equity) if account.last_equity is not None else None
            else:
                equity = (await compute_equity(db, account)).equity
            if equity is None:
                continue
            account.day_start_equity = equity
            account.day_start_at = now
            count += 1
        await log_event(db, "risk.daily_reset", accounts=count)
        await db.commit()
    return count
