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

import asyncio
import logging
import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.metrics import ORDERS, reason_label, safe_inc
from app.exchanges.base import ExchangeAdapter
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
from app.services.position_math import apply_fill
from app.services.risk_limits import aggregate_exposures, capital_cap
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


def bracket_params(brackets: list[dict], notional: Decimal) -> dict | None:
    """OrderRequest kwargs (maint margin rate, cum, max leverage) of the bracket that contains `notional`."""
    chosen = None
    for b in brackets:
        chosen = b
        if Decimal(str(b.get("notionalCap", "0") or "0")) > notional:
            break
    if not chosen or chosen.get("maintMarginRatio") in (None, ""):
        return None
    lev = chosen.get("initialLeverage")
    return {
        "maint_margin_rate": Decimal(str(chosen["maintMarginRatio"])),
        "maint_amount": Decimal(str(chosen.get("cum", "0") or "0")),
        "bracket_max_leverage": int(lev) if lev else None,
    }


def new_client_order_id() -> str:
    return f"btp-{uuid.uuid4().hex[:28]}"


_POSITIONS_KEY = "positions_by_symbol"


async def get_position(db: AsyncSession, account: Account, symbol: str) -> Position | None:
    """Net position of `account` in `symbol`, memoized per session.

    A tick looked the same row up 6-8 times (runner, OrderService, executor, position events). Re-selecting an
    object already in the identity map returns that same object without refreshing its attributes, so serving
    it from the session cache is equivalent, minus the round trip. Expired, deleted or detached objects (after a
    rollback, an explicit expire or a delete) are reloaded from the database.
    """
    cache: dict = db.sync_session.info.setdefault(_POSITIONS_KEY, {})
    key = (account.id, symbol)
    pos = cache.get(key)
    if pos is not None:
        state = sa_inspect(pos)
        if state.persistent and state.session_id == db.sync_session.hash_key and not state.expired_attributes:
            return pos
        cache.pop(key, None)
    pos = await db.scalar(select(Position).where(Position.account_id == account.id, Position.symbol == symbol))
    if pos is not None:
        cache[key] = pos
    return pos


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
        db.sync_session.info.setdefault(_POSITIONS_KEY, {})[(account.id, symbol)] = pos
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
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        take_profit: Decimal | None = None,
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
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        take_profit: Decimal | None = None,
        resting_order: Order | None = None,
    ) -> ExecutionResult:
        if order_type == OrderType.LIMIT and resting_order is None:
            if limit_price is None:
                return ExecutionResult(False, "limit_price_required")
            marketable = (side == OrderSide.BUY and ref_price <= limit_price) or (
                side == OrderSide.SELL and ref_price >= limit_price
            )
            if not marketable:
                order = Order(
                    workspace_id=account.workspace_id, account_id=account.id, bot_id=bot.id if bot else None,
                    symbol=symbol, side=side, type=OrderType.LIMIT, status=OrderStatus.NEW, quantity=quantity,
                    price=limit_price, stop_loss=stop_loss, take_profit=take_profit, leverage=leverage,
                    reduce_only=reduce_only, client_order_id=new_client_order_id(), is_paper=True, reason=tag or None,
                )  # fmt: skip
                db.add(order)
                await db.flush()
                return ExecutionResult(True, "resting", order, None, Decimal("0"))
            fill_price = ref_price  # marketable limit: fills at the (better) market price, no slippage
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

        if resting_order is not None:
            order = resting_order
            order.status, order.executed_qty, order.avg_price, order.quantity = (
                OrderStatus.FILLED, quantity, fill_price, quantity,
            )  # fmt: skip
        else:
            order = Order(
                workspace_id=account.workspace_id,
                account_id=account.id,
                bot_id=bot.id if bot else None,
                symbol=symbol,
                side=side,
                type=order_type,
                status=OrderStatus.FILLED,
                quantity=quantity,
                price=limit_price if order_type == OrderType.LIMIT else None,
                stop_loss=stop_loss,
                take_profit=take_profit,
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
            pos.take_profit = None
        elif not reduce_only:
            if stop_loss is not None:
                pos.stop_loss = stop_loss
            if take_profit is not None:
                pos.take_profit = take_profit
        account.paper_balance = Decimal(account.paper_balance) + outcome.realized_pnl - fee
        return ExecutionResult(True, "filled", order, fill_price, quantity, outcome.realized_pnl, fee)


class LiveExecutor:
    """Sends real MARKET orders (and a protective STOP_MARKET) through the account's exchange adapter."""

    is_paper = False

    def __init__(self, client: ExchangeAdapter) -> None:
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
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        take_profit: Decimal | None = None,
    ) -> ExecutionResult:
        is_limit = order_type == OrderType.LIMIT
        if is_limit and limit_price is None:
            return ExecutionResult(False, "limit_price_required")
        order = Order(
            workspace_id=account.workspace_id,
            account_id=account.id,
            bot_id=bot.id if bot else None,
            symbol=symbol,
            side=side,
            type=OrderType.LIMIT if is_limit else OrderType.MARKET,
            status=OrderStatus.PENDING,
            quantity=quantity,
            price=limit_price if is_limit else None,
            stop_loss=stop_loss,
            take_profit=take_profit,
            leverage=leverage,
            reduce_only=reduce_only,
            client_order_id=new_client_order_id(),
            is_paper=False,
            reason=tag or None,
        )
        db.add(order)
        await db.flush()
        extra: dict = {"price": limit_price, "time_in_force": "GTC"} if is_limit else {}
        resp = await self.client.place_order(
            symbol=symbol,
            side=side.value,
            order_type="LIMIT" if is_limit else "MARKET",
            quantity=quantity,
            reduce_only=reduce_only or None,
            client_order_id=order.client_order_id,
            newOrderRespType="RESULT",
            **extra,
        )
        order.exchange_order_id = str(resp.get("orderId"))
        order.status = OrderStatus(resp.get("status", "NEW"))
        order.executed_qty = Decimal(str(resp.get("executedQty", "0")))
        avg = Decimal(str(resp.get("avgPrice", "0") or "0"))
        order.avg_price = avg or None

        protective: list[Order] = []
        if stop_loss is not None and not reduce_only:
            protective.append(
                self._protective_order(
                    account, bot, symbol, side, quantity, leverage, tag, OrderType.STOP_MARKET, stop_loss
                )
            )
        if take_profit is not None and not reduce_only:
            protective.append(
                self._protective_order(
                    account, bot, symbol, side, quantity, leverage, tag, OrderType.TAKE_PROFIT_MARKET, take_profit
                )
            )
        if protective:
            errors = await self._place_protective(db, symbol, protective)
            stop_err = next((e for o, e in errors if o.type == OrderType.STOP_MARKET), None)
            if stop_err is not None:
                # Never leave a live entry without its stop: undo the entry (cancel / flatten) and fail loudly.
                for o in protective:
                    if o.type != OrderType.STOP_MARKET and o.status != OrderStatus.REJECTED:
                        await self._safe_cancel(symbol, o)  # a lone closePosition TP would linger
                undone = await self._undo_entry(order, symbol, protective[0].side)
                await log_event(
                    db, "order.protection_failed", level="critical", workspace_id=account.workspace_id,
                    account_id=account.id, symbol=symbol, side=side, quantity=quantity, error=str(stop_err)[:200],
                    entry_undone=undone,
                )  # fmt: skip
                return ExecutionResult(
                    False, f"protective_stop_failed: {stop_err}"[:255], order, avg or ref_price, order.executed_qty
                )
            for o, exc in errors:  # the stop is in place; a missing TP is not worth flattening the trade
                logger.warning("take profit placement failed: %s", exc)
                await log_event(
                    db, "order.take_profit_failed", level="warning", workspace_id=account.workspace_id,
                    account_id=account.id, symbol=symbol, error=str(exc)[:200],
                )  # fmt: skip
        # Fills (Trade rows) and positions are synchronized by the reconciler / user-data stream.
        return ExecutionResult(True, order.status.value, order, avg or ref_price, order.executed_qty)

    async def _undo_entry(self, order: Order, symbol: str, exit_side: OrderSide) -> str:
        """Best effort after a failed protective stop: cancel the rest of the entry and close what was filled."""
        done = []
        if order.status in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED, OrderStatus.PENDING):
            try:
                resp = await self.client.cancel_order(symbol, client_order_id=order.client_order_id)
                order.status = OrderStatus(resp.get("status", "CANCELED"))
                order.executed_qty = Decimal(str(resp.get("executedQty", order.executed_qty or 0)))
                done.append("canceled")
            except Exception as exc:  # pragma: no cover - logged, reconciler will catch up
                logger.error("could not cancel unprotected entry %s: %s", order.client_order_id, exc)
        filled = Decimal(order.executed_qty or 0)
        if filled > 0:
            try:
                await self.client.place_order(
                    symbol=symbol, side=exit_side.value, order_type="MARKET", quantity=filled, reduce_only=True,
                    client_order_id=new_client_order_id(),
                )  # fmt: skip
                done.append(f"closed {filled}")
            except Exception as exc:
                logger.critical("UNPROTECTED POSITION %s %s: close failed: %s", symbol, filled, exc)
                done.append("close_failed")
        return ",".join(done) or "nothing_to_undo"

    async def _safe_cancel(self, symbol: str, o: Order) -> None:
        try:
            await self.client.cancel_order(symbol, client_order_id=o.client_order_id)
            o.status = OrderStatus.CANCELED
        except Exception as exc:  # pragma: no cover - reconciler will catch up
            logger.error("could not cancel %s: %s", o.client_order_id, exc)

    @staticmethod
    def _protective_order(
        account: Account, bot: BotConfig | None, symbol: str, side: OrderSide, quantity: Decimal, leverage: int,
        tag: str, kind: OrderType, trigger: Decimal,
    ) -> Order:  # fmt: skip
        """Exchange-side STOP_MARKET / TAKE_PROFIT_MARKET (closePosition) protecting a live entry."""
        suffix = "stop_loss" if kind == OrderType.STOP_MARKET else "take_profit"
        return Order(
            workspace_id=account.workspace_id, account_id=account.id, bot_id=bot.id if bot else None, symbol=symbol,
            side=OrderSide.SELL if side == OrderSide.BUY else OrderSide.BUY, type=kind, status=OrderStatus.PENDING,
            quantity=quantity, stop_price=trigger, leverage=leverage, reduce_only=True,
            client_order_id=new_client_order_id(), is_paper=False, reason=f"{tag}:{suffix}",
        )  # fmt: skip

    async def _place_protective(
        self, db: AsyncSession, symbol: str, orders: list[Order]
    ) -> list[tuple[Order, BaseException]]:
        """Send SL and TP concurrently (independent closePosition orders): one exchange round trip instead of two.

        Rows are flushed first (clientOrderId persisted before anything reaches the exchange). Every order gets its
        response applied; failures are returned (marked REJECTED) so the caller decides how to react.
        """
        for o in orders:
            db.add(o)
        await db.flush()
        responses = await asyncio.gather(
            *(
                self.client.place_order(
                    symbol=symbol,
                    side=o.side.value,
                    order_type=o.type.value,
                    stop_price=o.stop_price,
                    close_position=True,
                    client_order_id=o.client_order_id,
                    workingType="MARK_PRICE",
                )  # fmt: skip
                for o in orders
            ),
            return_exceptions=True,
        )
        errors: list[tuple[Order, BaseException]] = []
        for o, resp in zip(orders, responses, strict=True):
            if isinstance(resp, BaseException):
                o.status, o.reason = OrderStatus.REJECTED, f"{o.reason} | {resp}"[:255]
                errors.append((o, resp))
                continue
            o.exchange_order_id = str(resp.get("orderId"))
            o.status = OrderStatus(resp.get("status", "NEW"))
        return errors


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
        order_type: OrderType = OrderType.MARKET,
        limit_price: Decimal | None = None,
        take_profit: Decimal | None = None,
        user_id: uuid.UUID | None = None,
    ) -> ExecutionResult:
        """`ref_price` is the expected entry: the market price, or the limit price for LIMIT orders."""
        filters = filters or default_filters(symbol)
        cap = capital_cap(account)
        if cap is not None and (risk_state.capital_cap is None or cap < risk_state.capital_cap):
            risk_state = replace(risk_state, capital_cap=cap)  # hard capital cap of live accounts (G-02)
        if order_type == OrderType.LIMIT:
            if limit_price is None or limit_price <= 0:
                return await self._reject(
                    db, account, bot, intent, symbol, Decimal("0"), leverage, "limit_price_required"
                )
            limit_price = filters.round_price(limit_price)
        if take_profit is not None and not intent.reduce_only:
            entry = limit_price if order_type == OrderType.LIMIT else ref_price
            wrong_side = take_profit <= entry if intent.side == OrderSide.BUY else take_profit >= entry
            if take_profit <= 0 or wrong_side:
                return await self._reject(
                    db, account, bot, intent, symbol, Decimal("0"), leverage,
                    "take_profit_invalid: long TP must be above entry, short TP below",
                )  # fmt: skip
            take_profit = filters.round_price(take_profit)

        if not intent.reduce_only and await is_global_kill_switch_on(db):
            return await self._reject(db, account, bot, intent, symbol, Decimal("0"), leverage, "global_kill_switch")

        qty = intent.quantity
        if qty is None:
            if intent.stop_loss is None:
                return await self._reject(
                    db, account, bot, intent, symbol, Decimal("0"), leverage, "stop_loss_required"
                )
            qty = self.rm.max_position_size(ref_price, intent.stop_loss, risk_state) * intent.risk_fraction
        is_market = order_type != OrderType.LIMIT
        qty = filters.round_qty(abs(qty), market=is_market)
        if qty <= 0:
            return await self._reject(db, account, bot, intent, symbol, qty, leverage, "size_zero_after_rounding")
        stop = filters.round_price(intent.stop_loss) if intent.stop_loss is not None else None

        risk_price = limit_price if order_type == OrderType.LIMIT and limit_price is not None else ref_price
        problem = filters.check(qty, risk_price, intent.reduce_only, market=is_market)
        if problem:
            return await self._reject(db, account, bot, intent, symbol, qty, leverage, problem)

        request = OrderRequest(
            symbol=symbol,
            side=intent.side,
            quantity=qty,
            entry_price=risk_price,
            stop_loss=stop,
            leverage=leverage,
            reduce_only=intent.reduce_only,
            **({} if intent.reduce_only else await self._bracket(symbol, qty * risk_price)),
        )
        if not intent.reduce_only:
            used = await self._used_margin(db, account, symbol, intent.side)
            if used:
                risk_state = replace(risk_state, used_margin=risk_state.used_margin + used)
        decision = self.rm.evaluate(request, risk_state)
        if decision.allowed and not intent.reduce_only:
            exposures = await aggregate_exposures(db, account, request, risk_state.equity)
            decision = self.rm.evaluate_aggregate(request, exposures)
        if not decision.allowed:
            result = await self._reject(db, account, bot, intent, symbol, qty, leverage, decision.reason)
            result.kill_switch = decision.kill_switch
            return result

        before_qty = await self._position_qty(db, account, symbol)
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
                **self._extra(order_type, limit_price, take_profit),
            )
        except Exception as exc:  # exchange errors -> rejected order, bot keeps running
            logger.exception("order execution failed")
            return await self._reject(db, account, bot, intent, symbol, qty, leverage, f"execution_error: {exc}"[:255])
        if result.ok:
            filled = self.executor.is_paper and result.reason == "filled"
            safe_inc(ORDERS, mode=self._mode, result="filled" if filled else "submitted", reason="none")
            await log_event(
                db,
                "order.filled" if filled else "order.submitted",
                user_id=user_id,
                workspace_id=account.workspace_id,
                account_id=account.id,
                symbol=symbol,
                side=intent.side,
                quantity=result.quantity,
                price=result.fill_price,
                tag=intent.tag,
                paper=self.executor.is_paper,
                mode=self._mode,
                order_type=order_type.value,
                limit_price=limit_price,
                stop_loss=stop,
                take_profit=take_profit,
            )
            if result.reason != "resting":
                await self._position_events(db, account, symbol, before_qty, result, leverage)
        else:
            await self._reject(db, account, bot, intent, symbol, qty, leverage, result.reason)
        return result

    async def _bracket(self, symbol: str, notional: Decimal) -> dict:
        """Maintenance margin (and max leverage) of the exchange bracket for `notional`, for the liquidation check.

        Live: the account's leverage brackets; paper or unavailable brackets: RISK_DEFAULT_MAINT_MARGIN_RATE.
        """
        default = {"maint_margin_rate": Decimal(str(settings.RISK_DEFAULT_MAINT_MARGIN_RATE))}
        client = getattr(self.executor, "client", None)
        if client is None or self.executor.is_paper:
            return default
        try:
            brackets = await client.get_leverage_brackets(symbol)
        except Exception as exc:  # unsupported adapter / API error: conservative default, never block on it
            logger.warning("leverage brackets unavailable for %s: %s", symbol, exc)
            return default
        return bracket_params(brackets, notional) or default

    @staticmethod
    async def _used_margin(db: AsyncSession, account: Account, symbol: str, side: OrderSide) -> Decimal:
        """Initial margin held by the account's open positions (a position this order reduces is not counted)."""
        rows = await db.scalars(select(Position).where(Position.account_id == account.id, Position.quantity != 0))
        used = Decimal("0")
        for p in rows:
            qty = Decimal(p.quantity)
            if p.symbol == symbol and (qty > 0) != (side == OrderSide.BUY):
                continue
            price = Decimal(p.mark_price or p.entry_price or 0)
            used += abs(qty) * price / Decimal(max(int(p.leverage or 1), 1))
        return used

    async def _position_qty(self, db: AsyncSession, account: Account, symbol: str) -> Decimal | None:
        """Paper positions change synchronously; live ones are tracked by the reconciler/user stream."""
        if not self.executor.is_paper:
            return None
        pos = await get_position(db, account, symbol)
        return Decimal(pos.quantity) if pos is not None else Decimal("0")

    async def _position_events(
        self,
        db: AsyncSession,
        account: Account,
        symbol: str,
        before: Decimal | None,
        result: ExecutionResult,
        leverage: int,
    ) -> None:
        if before is None:
            return
        after = await self._position_qty(db, account, symbol) or Decimal("0")
        ctx = {"workspace_id": account.workspace_id, "account_id": account.id, "symbol": symbol}
        if before != 0 and (after == 0 or (after > 0) != (before > 0)):
            await log_event(
                db, "position.closed", **ctx, side="LONG" if before > 0 else "SHORT", quantity=abs(before),
                price=result.fill_price, realized_pnl=result.realized_pnl,
            )  # fmt: skip
        if after != 0 and (before == 0 or (after > 0) != (before > 0)):
            await log_event(
                db, "position.opened", **ctx, side="LONG" if after > 0 else "SHORT", quantity=abs(after),
                price=result.fill_price, leverage=leverage,
            )  # fmt: skip

    @staticmethod
    def _extra(order_type: OrderType, limit_price: Decimal | None, take_profit: Decimal | None) -> dict:
        """Only pass the new kwargs when used, so custom executors with the original signature keep working."""
        extra: dict = {}
        if order_type != OrderType.MARKET:
            extra.update(order_type=order_type, limit_price=limit_price)
        if take_profit is not None:
            extra["take_profit"] = take_profit
        return extra

    @property
    def _mode(self) -> str:
        return "paper" if self.executor.is_paper else "live"

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
        safe_inc(ORDERS, mode=self._mode, result="rejected", reason=reason_label(reason))
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
