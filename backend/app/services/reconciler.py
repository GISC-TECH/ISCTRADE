"""Reconciler: keeps local orders, fills and positions consistent with Binance (live accounts).

Sources of truth, in order: user-data stream events (real time) and periodic REST polling (this module),
which also repairs anything missed while the stream was disconnected.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.metrics import RECONCILER_CHANGES, safe_inc
from app.exchanges.base import ERR_ORDER_NOT_FOUND, ExchangeAdapter, ExchangeAPIError
from app.models import Account, Order, OrderSide, OrderStatus, Position, Trade
from app.services.audit import log_event
from app.services.position_alerts import check_positions, liquidation_prices
from app.services.protection import ProtectionGuard

logger = logging.getLogger(__name__)

OPEN_STATUSES = (OrderStatus.PENDING, OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)
STOP_ORDER_TYPES = frozenset({"STOP_MARKET", "STOP", "TRAILING_STOP_MARKET"})


def _dec(value: Any, default: str = "0") -> Decimal:
    return Decimal(str(value if value not in (None, "") else default))


def _ms_to_dt(ms: int | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def needs_protection_check(event: dict[str, Any]) -> bool:
    """User-stream events after which the stop guarantee should run right away (not wait for the beat)."""
    etype = event.get("e")
    if etype == "ACCOUNT_UPDATE":
        return any(_dec(p.get("pa")) != 0 for p in event.get("a", {}).get("P", []))
    if etype == "ORDER_TRADE_UPDATE":
        o = event.get("o", {})
        protective = str(o.get("o") or o.get("ot") or "").upper() in STOP_ORDER_TYPES
        return protective and str(o.get("X", "")).upper() in ("CANCELED", "EXPIRED", "REJECTED")
    return etype == "ALGO_UPDATE"  # conditional (algo) order changed: cheap to re-check


class Reconciler:
    def __init__(self, db: AsyncSession, account: Account, client: ExchangeAdapter) -> None:
        self.db = db
        self.account = account
        self.client = client
        self.remote_positions: list[dict[str, Any]] | None = None  # non-flat, from the last sync_positions

    # ------------------------------------------------------------------ positions
    async def sync_positions(self, symbols: list[str] | None = None) -> int:
        from app.services.execution import get_or_create_position

        changed = 0
        rows = await self.client.get_positions(include_flat=True)
        self.remote_positions = [p for p in rows if _dec(p.get("positionAmt")) != 0]
        remote = {p["symbol"]: p for p in rows}
        if symbols is None:
            local = await self.db.scalars(
                select(Position.symbol).where(Position.account_id == self.account.id, Position.quantity != 0)
            )
            symbols = sorted({*(s for s, p in remote.items() if _dec(p.get("positionAmt")) != 0), *local})
        for symbol in symbols:
            p = remote.get(symbol)
            pos = await get_or_create_position(self.db, self.account, symbol)
            qty = _dec(p.get("positionAmt")) if p else Decimal(0)
            if pos.quantity != qty:
                changed += 1
            pos.quantity = qty
            pos.entry_price = _dec(p.get("entryPrice")) if p else Decimal(0)
            pos.mark_price = _dec(p.get("markPrice")) if p else None
            pos.unrealized_pnl = _dec(p.get("unRealizedProfit")) if p else Decimal(0)
            if p and p.get("leverage"):
                pos.leverage = int(p["leverage"])
            if qty == 0:
                pos.stop_loss = None
            pos.is_paper = False
        return changed

    # ------------------------------------------------------------------ orders
    async def sync_orders(self) -> int:
        rows = await self.db.scalars(
            select(Order).where(
                Order.account_id == self.account.id, Order.is_paper.is_(False), Order.status.in_(OPEN_STATUSES)
            )
        )
        updated = 0
        for order in rows:
            try:
                remote = await self.client.get_order(order.symbol, client_order_id=order.client_order_id)
            except ExchangeAPIError as exc:
                if exc.code == ERR_ORDER_NOT_FOUND:  # order does not exist
                    order.status = OrderStatus.REJECTED
                    order.reason = (order.reason or "") + " | not found on exchange"
                    updated += 1
                    continue
                raise
            if self._apply_remote_order(order, remote):
                updated += 1
        return updated

    @staticmethod
    def _apply_remote_order(order: Order, remote: dict[str, Any]) -> bool:
        before = (order.status, order.executed_qty)
        order.exchange_order_id = str(remote.get("orderId", order.exchange_order_id))
        order.status = OrderStatus(remote.get("status", order.status.value))
        order.executed_qty = _dec(remote.get("executedQty"), str(order.executed_qty))
        avg = _dec(remote.get("avgPrice"))
        if avg:
            order.avg_price = avg
        return before != (order.status, order.executed_qty)

    # ------------------------------------------------------------------ fills
    async def sync_trades(self, symbols: list[str]) -> int:
        inserted = 0
        for symbol in symbols:
            last = await self.db.scalar(
                select(func.max(Trade.executed_at)).where(
                    Trade.account_id == self.account.id, Trade.symbol == symbol, Trade.is_paper.is_(False)
                )
            )
            start = int(last.timestamp() * 1000) if last else None
            for t in await self.client.get_user_trades(symbol, start_time=start):
                if await self._insert_trade(
                    symbol=symbol,
                    trade_id=str(t["id"]),
                    exchange_order_id=str(t["orderId"]),
                    side=t["side"],
                    price=t["price"],
                    qty=t["qty"],
                    fee=t.get("commission", "0"),
                    fee_asset=t.get("commissionAsset", "USDT"),
                    realized=t.get("realizedPnl", "0"),
                    time_ms=t["time"],
                ):
                    inserted += 1
        return inserted

    async def _insert_trade(
        self, *, symbol, trade_id, exchange_order_id, side, price, qty, fee, fee_asset, realized, time_ms
    ) -> bool:
        exists = await self.db.scalar(
            select(Trade.id).where(Trade.account_id == self.account.id, Trade.exchange_trade_id == trade_id)
        )
        if exists:
            return False
        order = await self.db.scalar(
            select(Order).where(Order.account_id == self.account.id, Order.exchange_order_id == exchange_order_id)
        )
        self.db.add(
            Trade(
                workspace_id=self.account.workspace_id,
                account_id=self.account.id,
                order_id=order.id if order else None,
                bot_id=order.bot_id if order else None,
                symbol=symbol,
                side=OrderSide(side),
                price=_dec(price),
                quantity=_dec(qty),
                fee=abs(_dec(fee)),
                fee_asset=fee_asset or "USDT",
                realized_pnl=_dec(realized),
                exchange_trade_id=trade_id,
                is_paper=False,
                executed_at=_ms_to_dt(time_ms),
            )
        )
        await self.db.flush()
        return True

    # ------------------------------------------------------------------ user-data stream events
    async def handle_event(self, event: dict[str, Any]) -> None:
        etype = event.get("e")
        if etype == "ORDER_TRADE_UPDATE":
            o = event["o"]
            order = await self.db.scalar(
                select(Order).where(Order.account_id == self.account.id, Order.client_order_id == o.get("c"))
            )
            if order is not None:
                self._apply_remote_order(
                    order,
                    {"orderId": o.get("i"), "status": o.get("X"), "executedQty": o.get("z"), "avgPrice": o.get("ap")},
                )
            if o.get("x") == "TRADE":
                await self._insert_trade(
                    symbol=o["s"],
                    trade_id=str(o["t"]),
                    exchange_order_id=str(o["i"]),
                    side=o["S"],
                    price=o["L"],
                    qty=o["l"],
                    fee=o.get("n", "0"),
                    fee_asset=o.get("N", "USDT"),
                    realized=o.get("rp", "0"),
                    time_ms=o.get("T", event.get("E")),
                )
        elif etype == "ACCOUNT_UPDATE":
            from app.services.execution import get_or_create_position

            for p in event.get("a", {}).get("P", []):
                if p.get("ps", "BOTH") != "BOTH":
                    continue  # hedge mode not supported
                pos = await get_or_create_position(self.db, self.account, p["s"])
                pos.quantity = _dec(p.get("pa"))
                pos.entry_price = _dec(p.get("ep"))
                pos.unrealized_pnl = _dec(p.get("up"))
                pos.is_paper = False
                if pos.quantity == 0:
                    pos.stop_loss = None

    # ------------------------------------------------------------------ full pass
    async def run(self) -> dict[str, int]:
        positions = await self.sync_positions()
        orders = await self.sync_orders()
        symbols = sorted(
            set(
                await self.db.scalars(
                    select(Order.symbol).where(Order.account_id == self.account.id, Order.is_paper.is_(False))
                )
            )
        )
        trades = await self.sync_trades(symbols) if symbols else 0
        guard = await ProtectionGuard(self.db, self.account, self.client).run(self.remote_positions)
        live_positions = await self.db.scalars(
            select(Position).where(
                Position.account_id == self.account.id, Position.is_paper.is_(False), Position.quantity != 0
            )
        )
        await check_positions(self.db, self.account, list(live_positions), liquidation_prices(self.remote_positions))
        summary = {
            "positions_changed": positions, "orders_updated": orders, "trades_inserted": trades, **guard.as_dict(),
        }  # fmt: skip
        for kind, n in (("positions", positions), ("orders", orders), ("trades", trades)):
            if n:
                safe_inc(RECONCILER_CHANGES, n, kind=kind)
        if positions or orders or trades or guard.restored or guard.closed:
            await log_event(
                self.db, "reconcile.changes", workspace_id=self.account.workspace_id, account_id=self.account.id,
                **summary,
            )  # fmt: skip
        return summary
