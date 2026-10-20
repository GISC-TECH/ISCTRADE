"""Order execution pipeline.

    strategy intent ─▶ sizing/rounding ─▶ global kill switch ─▶ RiskManager ─▶ executor
                                                                   │
                                     reject (audit, maybe kill) ◀──┘

Executors:
- PaperExecutor (DEFAULT): simulated fills with fees and slippage, recorded in the DB with is_paper=True.
- LiveExecutor: real orders on Binance. Only used when settings.LIVE_TRADING_ENABLED is true AND the account
  is explicitly configured with execution_mode="live".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models import (
    Account,
    BotConfig,
    ExecutionMode,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Trade,
)
from app.models.base import utcnow
from app.services.audit import log_event
from app.services.binance_client import BinanceFuturesClient
from app.services.position_math import apply_fill
from app.services.risk_manager import AccountRiskState, OrderRequest, RiskManager
from app.services.symbol_filters import SymbolFilters, default_filters
from app.strategy.base import OrderIntent

logger = logging.getLogger(__name__)


class LiveTradingDisabled(RuntimeError):
    pass


@dataclass
class ExecutionResult:
    ok: bool
    reason: str
    order: Order | None = None
    fill_price: Decimal | None = None
    quantity: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    kill_switch: bool = False


def new_client_order_id() -> str:
    return f"btp-{uuid.uuid4().hex[:28]}"


async def get_position(db: AsyncSession, account: Account, symbol: str) -> Position | None:
    return await db.scalar(select(Position).where(Position.account_id == account.id, Position.symbol == symbol))


async def get_or_create_position(db: AsyncSession, account: Account, symbol: str, leverage: int = 1) -> Position:
    pos = await get_position(db, account, symbol)
    if pos is None:
        pos = Position(
            workspace_id=account.workspace_id,
            account_id=account.id,
            symbol=symbol,
            quantity=Decimal("0"),
            entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            leverage=leverage,
            is_paper=account.execution_mode == ExecutionMode.PAPER,
        )
        db.add(pos)
        await db.flush()
    return pos


class Executor(Protocol):
    is_paper: bool

    async def execute(
        self,
        db: AsyncSession,
        account: Account,
        bot: BotConfig | None,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        ref_price: Decimal,
        *,
        stop_loss: Decimal | None,
        reduce_only: bool,
        leverage: int,
        tag: str,
    ) -> ExecutionResult: ...


class PaperExecutor:
    """Simulated market fills at `ref_price` +/- slippage, taker fee charged on notional."""

    is_paper = True

    def __init__(self, fee_rate: Decimal | None = None, slippage_bps: Decimal | None = None) -> None:
        self.fee_rate = fee_rate if fee_rate is not None else Decimal(str(settings.PAPER_TAKER_FEE))
        self.slippage = (
            slippage_bps if slippage_bps is not None else Decimal(str(settings.PAPER_SLIPPAGE_BPS))
        ) / Decimal(10000)

    async def execute(
        self,
        db: AsyncSession,
        account: Account,
        bot: BotConfig | None,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        ref_price: Decimal,
        *,
        stop_loss: Decimal | None,
        reduce_only: bool,
        leverage: int,
        tag: str,
        fill_price: Decimal | None = None,
    ) -> ExecutionResult:
        pos = await get_or_create_position(db, account, symbol, leverage)
        if reduce_only:
            opposite = (pos.quantity > 0 and side == OrderSide.SELL) or (pos.quantity < 0 and side == OrderSide.BUY)
            if not opposite:
                return ExecutionResult(False, "reduce_only_no_position")
            quantity = min(quantity, abs(pos.quantity))

        if fill_price is None:
            slip = self.slippage if side == OrderSide.BUY else -self.slippage
            fill_price = ref_price * (1 + slip)
        outcome = apply_fill(pos.quantity, pos.entry_price, side, quantity, fill_price)
        fee = quantity * fill_price * self.fee_rate

        order = Order(
            workspace_id=account.workspace_id,
            account_id=account.id,
            bot_id=bot.id if bot else None,
            symbol=symbol,
            side=side,
            type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            quantity=quantity,
            price=None,
            stop_loss=stop_loss,
            leverage=leverage,
            reduce_only=reduce_only,
            executed_qty=quantity,
            avg_price=fill_price,
            client_order_id=new_client_order_id(),
            is_paper=True,
            reason=tag or None,
        )
        db.add(order)
        await db.flush()
        db.add(
            Trade(
                workspace_id=account.workspace_id,
                account_id=account.id,
                order_id=order.id,
                bot_id=bot.id if bot else None,
                symbol=symbol,
                side=side,
                price=fill_price,
                quantity=quantity,
                fee=fee,
                realized_pnl=outcome.realized_pnl,
                is_paper=True,
                executed_at=utcnow(),
            )
        )
        pos.quantity = outcome.quantity
        pos.entry_price = outcome.entry_price
        pos.mark_price = fill_price
        pos.unrealized_pnl = outcome.quantity * (fill_price - outcome.entry_price) if outcome.quantity else Decimal(0)
        pos.leverage = leverage
        if outcome.quantity == 0:
            pos.stop_loss = None
        elif stop_loss is not None and not reduce_only:
            pos.stop_loss = stop_loss
        account.paper_balance = Decimal(account.paper_balance) + outcome.realized_pnl - fee
        return ExecutionResult(True, "filled", order, fill_price, quantity, outcome.realized_pnl, fee)


class LiveExecutor:
    """Sends real MARKET orders (and a protective STOP_MARKET) to Binance USDT-M Futures."""

    is_paper = False

    def __init__(self, client: BinanceFuturesClient) -> None:
        if not settings.LIVE_TRADING_ENABLED:
            raise LiveTradingDisabled("LIVE_TRADING_ENABLED is false; refusing to create a live executor")
        self.client = client

    async def execute(
        self,
        db: AsyncSession,
        account: Account,
        bot: BotConfig | None,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        ref_price: Decimal,
        *,
        stop_loss: Decimal | None,
        reduce_only: bool,
        leverage: int,
        tag: str,
    ) -> ExecutionResult:
        order = Order(
            workspace_id=account.workspace_id,
            account_id=account.id,
            bot_id=bot.id if bot else None,
            symbol=symbol,
            side=side,
            type=OrderType.MARKET,
            status=OrderStatus.PENDING,
            quantity=quantity,
            stop_loss=stop_loss,
            leverage=leverage,
            reduce_only=reduce_only,
            client_order_id=new_client_order_id(),
            is_paper=False,
            reason=tag or None,
        )
        db.add(order)
        await db.flush()
        resp = await self.client.place_order(
            symbol=symbol,
            side=side.value,
            order_type="MARKET",
            quantity=quantity,
            reduce_only=reduce_only or None,
            client_order_id=order.client_order_id,
            newOrderRespType="RESULT",
        )
        order.exchange_order_id = str(resp.get("orderId"))
        order.status = OrderStatus(resp.get("status", "NEW"))
        order.executed_qty = Decimal(str(resp.get("executedQty", "0")))
        avg = Decimal(str(resp.get("avgPrice", "0") or "0"))
        order.avg_price = avg or None

        if stop_loss is not None and not reduce_only:
            stop_side = OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY
            stop = Order(
                workspace_id=account.workspace_id,
                account_id=account.id,
                bot_id=bot.id if bot else None,
                symbol=symbol,
                side=stop_side,
                type=OrderType.STOP_MARKET,
                status=OrderStatus.PENDING,
                quantity=quantity,
                stop_price=stop_loss,
                leverage=leverage,
                reduce_only=True,
                client_order_id=new_client_order_id(),
                is_paper=False,
                reason=f"{tag}:stop_loss",
            )
            db.add(stop)
            await db.flush()
            sresp = await self.client.place_order(
                symbol=symbol,
                side=stop_side.value,
                order_type="STOP_MARKET",
                stop_price=stop_loss,
                close_position=True,
                client_order_id=stop.client_order_id,
                workingType="MARK_PRICE",
            )
            stop.exchange_order_id = str(sresp.get("orderId"))
            stop.status = OrderStatus(sresp.get("status", "NEW"))
        # Fills (Trade rows) and positions are synchronized by the reconciler / user-data stream.
        return ExecutionResult(True, order.status.value, order, avg or ref_price, order.executed_qty)


async def is_global_kill_switch_on(db: AsyncSession) -> bool:
    from app.models.system import GLOBAL_KILL_SWITCH, SystemSetting

    row = await db.get(SystemSetting, GLOBAL_KILL_SWITCH)
    return bool(row and row.value.get("active"))


class OrderService:
    """Applies sizing, filters and risk rules, then delegates to the executor."""

    def __init__(self, executor: Executor, risk_manager: RiskManager | None = None) -> None:
        self.executor = executor
        self.rm = risk_manager or RiskManager()

    async def submit(
        self,
        db: AsyncSession,
        account: Account,
        intent: OrderIntent,
        *,
        symbol: str,
        ref_price: Decimal,
        leverage: int,
        risk_state: AccountRiskState,
        bot: BotConfig | None = None,
        filters: SymbolFilters | None = None,
    ) -> ExecutionResult:
        filters = filters or default_filters(symbol)
        user_id = None

        if not intent.reduce_only and await is_global_kill_switch_on(db):
            return await self._reject(db, account, bot, intent, symbol, Decimal("0"), leverage, "global_kill_switch")

        qty = intent.quantity
        if qty is None:
            if intent.stop_loss is None:
                return await self._reject(
                    db, account, bot, intent, symbol, Decimal("0"), leverage, "stop_loss_required"
                )
            qty = self.rm.max_position_size(ref_price, intent.stop_loss, risk_state) * intent.risk_fraction
        qty = filters.round_qty(abs(qty))
        if qty <= 0:
            return await self._reject(db, account, bot, intent, symbol, qty, leverage, "size_zero_after_rounding")
        stop = filters.round_price(intent.stop_loss) if intent.stop_loss is not None else None

        problem = filters.check(qty, ref_price, intent.reduce_only)
        if problem:
            return await self._reject(db, account, bot, intent, symbol, qty, leverage, problem)

        request = OrderRequest(
            symbol=symbol,
            side=intent.side,
            quantity=qty,
            entry_price=ref_price,
            stop_loss=stop,
            leverage=leverage,
            reduce_only=intent.reduce_only,
        )
        decision = self.rm.evaluate(request, risk_state)
        if not decision.allowed:
            result = await self._reject(db, account, bot, intent, symbol, qty, leverage, decision.reason)
            result.kill_switch = decision.kill_switch
            return result

        try:
            result = await self.executor.execute(
                db,
                account,
                bot,
                symbol,
                intent.side,
                qty,
                ref_price,
                stop_loss=stop,
                reduce_only=intent.reduce_only,
                leverage=leverage,
                tag=intent.tag,
            )
        except Exception as exc:  # exchange errors -> rejected order, bot keeps running
            logger.exception("order execution failed")
            return await self._reject(db, account, bot, intent, symbol, qty, leverage, f"execution_error: {exc}"[:255])
        if result.ok:
            await log_event(
                db,
                "order.filled" if self.executor.is_paper else "order.submitted",
                user_id=user_id,
                workspace_id=account.workspace_id,
                account_id=account.id,
                symbol=symbol,
                side=intent.side,
                quantity=result.quantity,
                price=result.fill_price,
                tag=intent.tag,
                paper=self.executor.is_paper,
            )
        else:
            await self._reject(db, account, bot, intent, symbol, qty, leverage, result.reason)
        return result

    async def _reject(
        self,
        db: AsyncSession,
        account: Account,
        bot: BotConfig | None,
        intent: OrderIntent,
        symbol: str,
        qty: Decimal,
        leverage: int,
        reason: str,
    ) -> ExecutionResult:
        order = Order(
            workspace_id=account.workspace_id,
            account_id=account.id,
            bot_id=bot.id if bot else None,
            symbol=symbol,
            side=intent.side,
            type=OrderType.MARKET,
            status=OrderStatus.REJECTED,
            quantity=qty,
            stop_loss=intent.stop_loss,
            leverage=leverage,
            reduce_only=intent.reduce_only,
            client_order_id=new_client_order_id(),
            is_paper=self.executor.is_paper,
            reason=reason[:255],
        )
        db.add(order)
        await log_event(
            db,
            "order.rejected",
            level="warning",
            workspace_id=account.workspace_id,
            account_id=account.id,
            symbol=symbol,
            side=intent.side,
            reason=reason,
            tag=intent.tag,
        )
        return ExecutionResult(False, reason, order)


def build_executor(account: Account) -> Executor:
    """Paper by default. Live only when globally enabled AND explicitly configured on the account."""
    if account.execution_mode != ExecutionMode.LIVE:
        return PaperExecutor()
    if not settings.LIVE_TRADING_ENABLED:
        raise LiveTradingDisabled("account is in live mode but LIVE_TRADING_ENABLED is false")
    from app.services.accounts import client_for_account

    return LiveExecutor(client_for_account(account))
