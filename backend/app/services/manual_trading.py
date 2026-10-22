"""Manual trading from the real-time panel.

Every opening order goes through the same pipeline as the bots: global kill switch -> symbol filters ->
RiskManager -> executor of the account's mode (paper by default; live only when LIVE_TRADING_ENABLED and the
account is explicitly in live mode). A stop loss is mandatory for openings; take profit is optional.
Reduce-only actions (close, close all, cancel) are always allowed, even under a kill switch.
Every action is audited (`manual.*`), which also triggers the `manual_action` alert and a real-time event.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.exchanges.registry import public_adapter
from app.models import (
    Account,
    AccountStatus,
    ExecutionMode,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
)
from app.services import accounts as accounts_service
from app.services import prices
from app.services.audit import log_event
from app.services.equity import compute_equity, open_positions, refresh_paper_marks, roll_risk_baselines
from app.services.execution import (
    ExecutionResult,
    LiveExecutor,
    OrderService,
    PaperExecutor,
    get_position,
    is_global_kill_switch_on,
)
from app.services.kill_switch import trigger_account_kill_switch
from app.services.position_mode import HedgeModeUnsupported, ensure_one_way
from app.services.risk_manager import RiskManager
from app.services.symbol_filters import SymbolFilters, default_filters
from app.strategy.base import OrderIntent

logger = logging.getLogger(__name__)
PriceSource = Callable[[str, bool], Awaitable[Decimal | None]]


async def _default_price(symbol: str, testnet: bool) -> Decimal | None:
    return await prices.get_price(symbol, testnet)


_FILTERS_TTL = 3600.0
_filters_cache: dict[tuple[str, bool], tuple[float, SymbolFilters]] = {}


async def _default_filters(symbol: str, testnet: bool) -> SymbolFilters:
    """Exchange filters (tick/step size, min notional) from public exchangeInfo, cached for an hour."""
    key = (symbol, testnet)
    cached = _filters_cache.get(key)
    if cached and time.monotonic() - cached[0] < _FILTERS_TTL:
        return cached[1]
    try:
        async with public_adapter(testnet) as client:
            filters = await client.get_symbol_filters(symbol)
    except Exception as exc:
        logger.warning("symbol filters unavailable for %s (%s); using defaults", symbol, type(exc).__name__)
        return default_filters(symbol)
    _filters_cache[key] = (time.monotonic(), filters)
    return filters


# Indirections so tests can mock market data and the exchange client.
price_source: PriceSource = _default_price
filters_source: Callable[[str, bool], Awaitable[SymbolFilters]] = _default_filters
client_factory = accounts_service.client_for_account

OPEN_ORDER_STATUSES = (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING)


class ManualTradingError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class ManualOrderRequest:
    symbol: str
    side: OrderSide
    type: OrderType
    quantity: Decimal
    stop_loss: Decimal
    price: Decimal | None = None
    take_profit: Decimal | None = None
    leverage: int | None = None


def _is_live(account: Account) -> bool:
    return account.execution_mode == ExecutionMode.LIVE


def _check_live_enabled(account: Account) -> None:
    if _is_live(account) and not settings.LIVE_TRADING_ENABLED:
        raise ManualTradingError(409, "LIVE_TRADING_ENABLED=false: conta em modo live não pode ser operada")


async def _audit(db: AsyncSession, action: str, account: Account, user_id: uuid.UUID | None, **data: Any) -> None:
    await log_event(
        db, f"manual.{action}", user_id=user_id, workspace_id=account.workspace_id, account_id=account.id,
        mode=account.execution_mode.value, **data,
    )  # fmt: skip


async def _price(account: Account, symbol: str) -> Decimal:
    price = await price_source(symbol, account.is_testnet)
    if price is None or price <= 0:
        raise ManualTradingError(503, f"Preço indisponível para {symbol}")
    return Decimal(price)


async def _assert_can_open(db: AsyncSession, account: Account) -> None:
    if account.status == AccountStatus.KILLED:
        raise ManualTradingError(409, "Kill switch da conta ativo: apenas fechamento de posições é permitido")
    if await is_global_kill_switch_on(db):
        raise ManualTradingError(409, "Kill switch global ativo: apenas fechamento de posições é permitido")


async def place_order(
    db: AsyncSession, account: Account, req: ManualOrderRequest, user_id: uuid.UUID | None
) -> ExecutionResult:
    symbol = req.symbol.upper()
    try:
        await _assert_can_open(db, account)
    except ManualTradingError as exc:
        await _audit(db, "order_blocked", account, user_id, symbol=symbol, side=req.side, reason=exc.detail)
        raise
    if req.type not in (OrderType.MARKET, OrderType.LIMIT):
        raise ManualTradingError(422, "Tipo de ordem inválido")
    if req.type == OrderType.LIMIT and (req.price is None or req.price <= 0):
        raise ManualTradingError(422, "Ordem LIMIT exige preço")

    _check_live_enabled(account)
    market_price = await _price(account, symbol)
    pos = await get_position(db, account, symbol)
    leverage = req.leverage or (pos.leverage if pos is not None and pos.quantity != 0 else 1)

    client = client_factory(account) if _is_live(account) else None
    try:
        if client is not None:
            try:
                await ensure_one_way(client, account.id)
            except HedgeModeUnsupported as exc:
                raise ManualTradingError(409, str(exc)) from None
        filters = (
            await client.get_symbol_filters(symbol) if client else await filters_source(symbol, account.is_testnet)
        )
        if not _is_live(account):
            await refresh_paper_marks(db, account, {symbol: market_price})
        equity = (await compute_equity(db, account, client)).equity
        roll_risk_baselines(account, equity)
        rm = RiskManager()
        risk_state = rm.from_account(account, equity)
        if rm.is_kill_switch_triggered(risk_state):
            await trigger_account_kill_switch(db, account, f"drawdown {rm.drawdown_pct(risk_state):.2f}%")
            raise ManualTradingError(409, "Drawdown máximo atingido: kill switch da conta acionado")
        executor = LiveExecutor(client) if client is not None else PaperExecutor()
        intent = OrderIntent(side=req.side, quantity=req.quantity, stop_loss=req.stop_loss, tag="manual")
        result = await OrderService(executor, rm).submit(
            db, account, intent, symbol=symbol,
            ref_price=market_price,
            leverage=leverage, risk_state=risk_state, filters=filters, order_type=req.type,
            limit_price=req.price if req.type == OrderType.LIMIT else None, take_profit=req.take_profit,
            user_id=user_id,
        )  # fmt: skip
        if result.kill_switch:
            await trigger_account_kill_switch(db, account, result.reason)
    finally:
        if client is not None:
            await client.close()
    await _audit(
        db, "order", account, user_id, symbol=symbol, side=req.side, order_type=req.type.value,
        quantity=result.quantity or req.quantity, price=result.fill_price or req.price, stop_loss=req.stop_loss,
        take_profit=req.take_profit, leverage=leverage, result="ok" if result.ok else "rejected",
        reason=result.reason,
    )  # fmt: skip
    return result


async def close_position(
    db: AsyncSession, account: Account, symbol: str, user_id: uuid.UUID | None, *, action: str = "close"
) -> ExecutionResult | None:
    """Reduce-only MARKET for the whole position and cancel the symbol's open orders (SL/TP/limits)."""
    symbol = symbol.upper()
    await cancel_symbol_orders(db, account, symbol)
    if _is_live(account):
        _check_live_enabled(account)
        async with client_factory(account) as client:
            await client.cancel_all_orders(symbol)
            rows = [p for p in await client.get_positions(symbol) if Decimal(str(p.get("positionAmt", "0"))) != 0]
            if not rows:
                return None
            qty = Decimal(str(rows[0]["positionAmt"]))
            ref = Decimal(str(rows[0].get("markPrice") or "0")) or await _price(account, symbol)
            result = await _reduce(db, account, LiveExecutor(client), symbol, qty, ref, user_id)
    else:
        pos = await get_position(db, account, symbol)
        if pos is None or pos.quantity == 0:
            return None
        qty = Decimal(pos.quantity)
        try:
            ref = await _price(account, symbol)
        except ManualTradingError:
            ref = Decimal(pos.mark_price or pos.entry_price)
        result = await _reduce(db, account, PaperExecutor(), symbol, qty, ref, user_id)
    if action == "close":
        await _audit(
            db, "close", account, user_id, symbol=symbol, quantity=abs(qty), price=result.fill_price,
            realized_pnl=result.realized_pnl, result="ok" if result.ok else "rejected", reason=result.reason,
        )  # fmt: skip
    return result


async def _reduce(db, account, executor, symbol: str, qty: Decimal, ref: Decimal, user_id) -> ExecutionResult:
    side = OrderSide.SELL if qty > 0 else OrderSide.BUY
    intent = OrderIntent(side=side, quantity=abs(qty), reduce_only=True, tag="manual_close")
    rm = RiskManager()
    client = getattr(executor, "client", None)
    equity = (await compute_equity(db, account, client)).equity
    pos = await get_position(db, account, symbol)
    filters = await client.get_symbol_filters(symbol) if client else await filters_source(symbol, account.is_testnet)
    return await OrderService(executor, rm).submit(
        db, account, intent, symbol=symbol, ref_price=ref, leverage=pos.leverage if pos else 1,
        risk_state=rm.from_account(account, equity), filters=filters, user_id=user_id,
    )  # fmt: skip


async def close_all(db: AsyncSession, account: Account, user_id: uuid.UUID | None) -> dict[str, Any]:
    if _is_live(account):
        _check_live_enabled(account)
        async with client_factory(account) as client:
            symbols = [p["symbol"] for p in await client.get_positions()]
    else:
        symbols = [p.symbol for p in await open_positions(db, account)]
    canceled = await cancel_symbol_orders(db, account, None)
    closed, failed = [], []
    for symbol in symbols:
        res = await close_position(db, account, symbol, user_id, action="close_all")
        (closed if res is None or res.ok else failed).append(symbol)
    await _audit(db, "close_all", account, user_id, closed=len(closed), failed=failed, orders_canceled=canceled)
    return {"closed": closed, "failed": failed, "orders_canceled": canceled}


async def cancel_symbol_orders(db: AsyncSession, account: Account, symbol: str | None) -> int:
    """Cancel local open orders (paper resting limits; live rows are also canceled on the exchange)."""
    q = select(Order).where(Order.account_id == account.id, Order.status.in_(OPEN_ORDER_STATUSES))
    if symbol:
        q = q.where(Order.symbol == symbol)
    rows = list(await db.scalars(q))
    if _is_live(account) and rows and settings.LIVE_TRADING_ENABLED:
        async with client_factory(account) as client:
            for sym in {o.symbol for o in rows}:
                await client.cancel_all_orders(sym)
    for o in rows:
        o.status = OrderStatus.CANCELED
    return len(rows)


async def cancel_order(db: AsyncSession, account: Account, order_id: uuid.UUID, user_id: uuid.UUID | None) -> Order:
    order = await db.scalar(select(Order).where(Order.id == order_id, Order.account_id == account.id))
    if order is None:
        raise ManualTradingError(404, "Ordem não encontrada")
    if order.status not in OPEN_ORDER_STATUSES:
        raise ManualTradingError(409, f"Ordem não está aberta ({order.status.value})")
    if not order.is_paper:
        if not settings.LIVE_TRADING_ENABLED:
            raise ManualTradingError(409, "LIVE_TRADING_ENABLED=false: conta em modo live não pode ser operada")
        async with client_factory(account) as client:
            await client.cancel_order(order.symbol, client_order_id=order.client_order_id)
    order.status = OrderStatus.CANCELED
    await _audit(
        db, "cancel", account, user_id, symbol=order.symbol, side=order.side, order_type=order.type.value,
        quantity=order.quantity, price=order.price or order.stop_price,
    )  # fmt: skip
    return order


async def set_leverage(
    db: AsyncSession, account: Account, symbol: str, leverage: int, user_id: uuid.UUID | None
) -> int:
    symbol = symbol.upper()
    max_lev = min(int(account.max_leverage), int(settings.RISK_MAX_LEVERAGE_CAP))
    if leverage < 1 or leverage > max_lev:
        raise ManualTradingError(422, f"Alavancagem fora do limite da conta (1..{max_lev}x)")
    await _assert_can_open(db, account)
    if _is_live(account):
        _check_live_enabled(account)
        async with client_factory(account) as client:
            await client.set_leverage(symbol, leverage)
    else:
        pos = await get_position(db, account, symbol)
        if pos is not None:
            pos.leverage = leverage
    await _audit(db, "leverage", account, user_id, symbol=symbol, leverage=leverage)
    return leverage
