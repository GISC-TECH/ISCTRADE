"""Paper (simulated) order book: resting LIMIT orders and position SL/TP checks against the mark price.

Runs every PAPER_ORDER_CHECK_SECONDS (Celery beat `paper.process_orders`). Live accounts are not touched:
their LIMIT/STOP_MARKET/TAKE_PROFIT_MARKET orders live on Binance and are synchronized by the reconciler.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import Account, AccountStatus, ExecutionMode, Order, OrderSide, OrderStatus, OrderType, Position
from app.services import prices
from app.services.audit import log_event
from app.services.equity import open_positions, refresh_paper_marks
from app.services.execution import PaperExecutor, is_global_kill_switch_on
from app.services.position_alerts import check_positions

logger = logging.getLogger(__name__)
PriceSource = Callable[[str, bool], Awaitable[Decimal | None]]


async def _default_price(symbol: str, testnet: bool) -> Decimal | None:
    return await prices.get_price(symbol, testnet)


price_source: PriceSource = _default_price


@dataclass
class PaperOrderReport:
    filled: int = 0
    canceled: int = 0
    partial: int = 0
    stops: int = 0
    take_profits: int = 0
    errors: list[str] = field(default_factory=list)


async def _default_volume(symbol: str, testnet: bool) -> Decimal | None:
    """Volume (base asset) of the last closed 1m candle: the liquidity reference for paper LIMIT fills."""
    from app.exchanges.registry import public_adapter

    client = public_adapter(testnet)
    try:
        rows = await client.get_klines(symbol, "1m", limit=2)
    except Exception as exc:  # noqa: BLE001 - unknown liquidity -> uncapped fill (historical behaviour)
        logger.warning("paper volume reference unavailable for %s: %s", symbol, exc)
        return None
    finally:
        await client.close()
    return Decimal(str(rows[-2][5])) if len(rows) >= 2 else None


VolumeSource = Callable[[str, bool], Awaitable[Decimal | None]]
volume_source: VolumeSource = _default_volume


def limit_crossed(side: OrderSide, limit: Decimal, price: Decimal, through_bps: Decimal = Decimal(0)) -> bool:
    """A paper LIMIT fills once the price touches it, or trades ``through_bps`` beyond it (queue-position proxy)."""
    margin = limit * through_bps / Decimal(10000)
    return price <= limit - margin if side == OrderSide.BUY else price >= limit + margin


def exit_trigger(pos: Position, price: Decimal) -> str | None:
    qty = Decimal(pos.quantity)
    if qty == 0:
        return None
    long = qty > 0
    if pos.stop_loss is not None and (price <= pos.stop_loss if long else price >= pos.stop_loss):
        return "stop_loss"
    if pos.take_profit is not None and (price >= pos.take_profit if long else price <= pos.take_profit):
        return "take_profit"
    return None


async def process_account(db: AsyncSession, account: Account, *, global_kill: bool, report: PaperOrderReport) -> None:
    executor = PaperExecutor()
    resting = list(
        await db.scalars(
            select(Order).where(
                Order.account_id == account.id, Order.is_paper.is_(True),
                Order.status.in_((OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)), Order.type == OrderType.LIMIT,
            )
        )
    )  # fmt: skip
    positions = list(
        await db.scalars(
            select(Position).where(
                Position.account_id == account.id, Position.quantity != 0,
                or_(Position.stop_loss.is_not(None), Position.take_profit.is_not(None)),
            )
        )
    )  # fmt: skip
    symbols = {o.symbol for o in resting} | {p.symbol for p in positions}
    if not symbols:
        return
    ordered = sorted(symbols)
    # Independent lookups (Redis cache, REST fallback on a miss): concurrently instead of one RTT per symbol.
    found = await asyncio.gather(*(price_source(symbol, account.is_testnet) for symbol in ordered))
    marks: dict[str, Decimal] = {
        symbol: Decimal(price) for symbol, price in zip(ordered, found, strict=True) if price is not None and price > 0
    }
    blocked = global_kill or account.status == AccountStatus.KILLED
    through = Decimal(str(settings.PAPER_LIMIT_THROUGH_BPS))
    volumes: dict[str, Decimal | None] = {}

    for order in resting:
        if blocked and not order.reduce_only:
            order.status = OrderStatus.CANCELED
            order.reason = "kill_switch"
            report.canceled += 1
            await log_event(
                db, "order.canceled", level="warning", workspace_id=account.workspace_id, account_id=account.id,
                symbol=order.symbol, side=order.side, reason="kill_switch",
            )  # fmt: skip
            continue
        price = marks.get(order.symbol)
        if price is None or order.price is None or not limit_crossed(order.side, Decimal(order.price), price, through):
            continue
        cap = None
        if executor.max_volume_pct > 0 and not order.reduce_only:
            if order.symbol not in volumes:
                volumes[order.symbol] = await volume_source(order.symbol, account.is_testnet)
            cap = executor.volume_cap(order.symbol, volumes[order.symbol])
        remaining = Decimal(order.quantity) - Decimal(order.executed_qty or 0)
        res = await executor.execute(
            db, account, None, order.symbol, order.side, remaining, Decimal(order.price),
            stop_loss=order.stop_loss, take_profit=order.take_profit, reduce_only=order.reduce_only,
            leverage=order.leverage, tag=order.reason or "limit", fill_price=Decimal(order.price),
            order_type=OrderType.LIMIT, limit_price=Decimal(order.price), resting_order=order, max_fill_qty=cap,
        )  # fmt: skip
        if res.ok and order.status == OrderStatus.PARTIALLY_FILLED:
            report.partial += 1
        elif res.ok:
            report.filled += 1
            await log_event(
                db, "order.filled", workspace_id=account.workspace_id, account_id=account.id, symbol=order.symbol,
                side=order.side, quantity=res.quantity, price=res.fill_price, tag=order.reason, paper=True,
                mode="paper", order_type="LIMIT",
            )  # fmt: skip
        else:
            order.status, order.reason = OrderStatus.CANCELED, res.reason[:255]
            report.canceled += 1

    # Flush the LIMIT fills first: refresh() discards unflushed changes, which silently dropped a fill that added to a
    # position already holding SL/TP (the trade row was kept, the position quantity was not).
    await db.flush()
    for pos in positions:
        await db.refresh(pos)
        price = marks.get(pos.symbol)
        trigger = exit_trigger(pos, price) if price is not None else None
        if trigger is None:
            continue
        qty = Decimal(pos.quantity)
        side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        res = await executor.execute(
            db, account, None, pos.symbol, side, abs(qty), price, stop_loss=None, reduce_only=True,
            leverage=pos.leverage, tag=trigger,
        )  # fmt: skip
        if not res.ok:
            continue
        if trigger == "stop_loss":
            report.stops += 1
        else:
            report.take_profits += 1
        await log_event(
            db, f"position.{trigger}", level="warning" if trigger == "stop_loss" else "info",
            workspace_id=account.workspace_id, account_id=account.id, symbol=pos.symbol,
            side="LONG" if qty > 0 else "SHORT", quantity=abs(qty), price=res.fill_price,
            realized_pnl=res.realized_pnl,
        )  # fmt: skip
    await refresh_paper_marks(db, account, marks)
    await check_positions(db, account, await open_positions(db, account))  # G-06: PnL / near stop / near liq.


async def process_paper_orders(db: AsyncSession) -> PaperOrderReport:
    report = PaperOrderReport()
    global_kill = await is_global_kill_switch_on(db)
    accounts = list(await db.scalars(select(Account).where(Account.execution_mode == ExecutionMode.PAPER)))
    for account in accounts:
        try:
            async with db.begin_nested():
                await process_account(db, account, global_kill=global_kill, report=report)
        except Exception as exc:
            logger.exception("paper order processing failed for account %s", account.id)
            report.errors.append(f"{account.id}: {type(exc).__name__}")
    await db.commit()
    return report
