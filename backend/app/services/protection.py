"""Stop guarantee for live positions (G-01).

Run by the reconciler after positions/orders are synchronized (beat, every RECONCILE_SECONDS; on worker start;
and right after user-stream events that can leave a position naked). For every open position at the exchange:

1. protected  - a reduce-only/closePosition STOP (STOP_MARKET, STOP, TRAILING_STOP_MARKET; regular or algo
                orders) on the exit side covers the whole quantity -> its trigger is remembered as the stop;
2. restore    - otherwise the stop is re-created: the stored stop (``Position.stop_loss``, else the stop of the
                latest local order of that side) or, for positions without a known stop (opened outside the
                platform), an emergency stop (ATR or percent from the mark, never beyond the liquidation price)
                when ``PROTECTION_ADOPT_EXTERNAL=stop``;
3. crossed    - a stored stop the mark already went through cannot be placed (it would trigger at once): the
                position is closed reduce-only (``PROTECTION_CLOSE_IF_STOP_CROSSED``) or gets an emergency stop;
4. alert      - whenever a position stays without a stop (placement failed, policy ``alert``, live trading
                disabled, open orders unreadable), a critical ``position.unprotected`` event is recorded, which
                becomes the "Posição desprotegida" alert (WhatsApp/Telegram/e-mail, throttled per symbol).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.exchanges.base import ExchangeAdapter
from app.models import Account, Order, OrderSide, OrderType, Position
from app.models.base import utcnow
from app.services.audit import log_event

logger = logging.getLogger(__name__)

STOP_TYPES = frozenset({"STOP_MARKET", "STOP", "TRAILING_STOP_MARKET"})
ACTIVE = frozenset({"NEW", "PARTIALLY_FILLED", "PENDING"})


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except Exception:
        return Decimal("0")


def _true(value: Any) -> bool:
    return value is True or str(value).lower() == "true"


@dataclass
class Coverage:
    full: bool
    trigger: Decimal | None = None  # closest protective trigger price (None for trailing stops)


def stop_coverage(orders: list[dict], symbol: str, qty: Decimal) -> Coverage:
    """Do the open orders hold a protective stop for the whole position?"""
    exit_side = "SELL" if qty > 0 else "BUY"
    covered, full, best = Decimal("0"), False, None
    for o in orders:
        if o.get("symbol") != symbol or str(o.get("side", "")).upper() != exit_side:
            continue
        if str(o.get("type") or o.get("orderType") or "").upper() not in STOP_TYPES:
            continue
        if str(o.get("status") or "NEW").upper() not in ACTIVE:
            continue
        close, reduce = _true(o.get("closePosition")), _true(o.get("reduceOnly"))
        if not (close or reduce):
            continue  # a plain stop could open a position on the other side; it does not protect
        remaining = _dec(o.get("origQty") or o.get("quantity")) - _dec(o.get("executedQty"))
        if close or remaining <= 0:  # closePosition (Binance) / qty 0 + closeOnTrigger (Bybit)
            full = True
        else:
            covered += remaining
        trigger = _dec(o.get("stopPrice") or o.get("triggerPrice"))
        if trigger > 0 and (best is None or (trigger > best if qty > 0 else trigger < best)):
            best = trigger
    return Coverage(full or covered >= abs(qty), best)


@dataclass
class GuardSummary:
    protected: int = 0
    restored: int = 0
    unprotected: int = 0
    closed: int = 0
    skipped: int = 0
    details: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, int]:
        return {
            "stops_ok": self.protected, "stops_restored": self.restored, "unprotected": self.unprotected,
            "closed_crossed": self.closed,
        }  # fmt: skip


class ProtectionGuard:
    def __init__(self, db: AsyncSession, account: Account, client: ExchangeAdapter) -> None:
        self.db, self.account, self.client = db, account, client

    async def run(self, remote_positions: list[dict] | None = None) -> GuardSummary:
        summary = GuardSummary()
        if not settings.PROTECTION_GUARD_ENABLED:
            return summary
        if remote_positions is None:
            remote_positions = await self.client.get_positions()
        positions = [
            p for p in remote_positions
            if _dec(p.get("positionAmt")) != 0 and str(p.get("positionSide") or "BOTH") == "BOTH"
        ]  # fmt: skip
        if not positions:
            return summary
        try:
            open_orders = await self.client.get_open_orders()
        except Exception as exc:
            for p in positions:
                await self._unprotected(
                    summary, p, f"não foi possível listar as ordens abertas: {exc}"[:200], level="warning"
                )
            return summary
        for p in positions:
            try:
                await self._protect(summary, p, open_orders)
            except Exception as exc:  # one symbol must never stop the others
                logger.exception("protection guard failed for %s", p.get("symbol"))
                await self._unprotected(summary, p, f"erro no guardião de stop: {exc}"[:200])
        return summary

    # ------------------------------------------------------------------ per position
    async def _protect(self, summary: GuardSummary, p: dict, open_orders: list[dict]) -> None:
        from app.services.execution import get_or_create_position

        symbol, qty = p["symbol"], _dec(p.get("positionAmt"))
        mark = _dec(p.get("markPrice")) or _dec(p.get("entryPrice"))
        pos = await get_or_create_position(self.db, self.account, symbol)
        cov = stop_coverage(open_orders, symbol, qty)
        if cov.full:
            if cov.trigger is not None:
                pos.stop_loss = cov.trigger
            summary.protected += 1
            return
        if await self._recent_local_order(symbol):
            summary.skipped += 1  # an entry/stop of ours is in flight; the next pass decides
            return

        stop, source = await self._stored_stop(pos, symbol, qty)
        if stop is not None and mark > 0 and (stop >= mark if qty > 0 else stop <= mark):
            if settings.PROTECTION_CLOSE_IF_STOP_CROSSED:
                await self._close_crossed(summary, p, pos, stop, mark)
                return
            stop, source = None, "emergency_after_crossed"
        if stop is None:
            if source == "none" and settings.PROTECTION_ADOPT_EXTERNAL != "stop":
                await self._unprotected(
                    summary, p, "posição aberta fora da plataforma sem stop (PROTECTION_ADOPT_EXTERNAL=alert)",
                    external=True,
                )  # fmt: skip
                return
            stop = await self.emergency_stop(symbol, qty, mark, _dec(p.get("liquidationPrice")))
            source = "emergency" if source == "none" else source
            if stop is None:
                await self._unprotected(summary, p, "não foi possível calcular um stop de emergência")
                return
        if not settings.LIVE_TRADING_ENABLED:
            await self._unprotected(summary, p, "LIVE_TRADING_ENABLED=false: o stop não pode ser recolocado")
            return

        from app.services.execution import LiveExecutor

        try:
            filters = await self.client.get_symbol_filters(symbol)
            stop = filters.round_price(stop)
        except Exception as exc:  # pragma: no cover - rounding is best effort
            logger.warning("symbol filters unavailable for %s: %s", symbol, exc)
        leverage = int(_dec(p.get("leverage")) or pos.leverage or 1)
        order, error = await LiveExecutor(self.client).place_protective_stop(
            self.db, self.account, symbol, qty, stop, leverage=leverage, tag=f"guard_{source}"
        )
        if error is not None:
            await self._unprotected(summary, p, f"falha ao recolocar o stop {stop}: {error}"[:200], stop=stop)
            return
        pos.stop_loss = stop
        summary.restored += 1
        summary.details.append({"symbol": symbol, "stop": str(stop), "source": source})
        await log_event(
            self.db, "position.stop_restored", level="warning", workspace_id=self.account.workspace_id,
            account_id=self.account.id, symbol=symbol, side="LONG" if qty > 0 else "SHORT", quantity=abs(qty),
            price=mark, stop_loss=stop, source=source, external=source.startswith("emergency"),
            client_order_id=order.client_order_id,
        )  # fmt: skip

    async def _recent_local_order(self, symbol: str) -> bool:
        if settings.PROTECTION_GRACE_SECONDS <= 0:
            return False
        since = utcnow() - timedelta(seconds=settings.PROTECTION_GRACE_SECONDS)
        found = await self.db.scalar(
            select(Order.id).where(
                Order.account_id == self.account.id, Order.symbol == symbol, Order.is_paper.is_(False),
                Order.created_at >= since,
            ).limit(1)
        )  # fmt: skip
        return found is not None

    async def _stored_stop(self, pos: Position, symbol: str, qty: Decimal) -> tuple[Decimal | None, str]:
        """(stop, source): the position's remembered stop, else the stop of the latest local order of its side."""
        if pos.stop_loss is not None and Decimal(pos.stop_loss) > 0:
            return Decimal(pos.stop_loss), "stored"
        entry_side = OrderSide.BUY if qty > 0 else OrderSide.SELL
        exit_side = OrderSide.SELL if qty > 0 else OrderSide.BUY
        since = utcnow() - timedelta(days=settings.PROTECTION_ORDER_HISTORY_DAYS)
        order = await self.db.scalar(
            select(Order)
            .where(
                Order.account_id == self.account.id, Order.symbol == symbol, Order.is_paper.is_(False),
                Order.created_at >= since,
                or_(
                    (Order.type == OrderType.STOP_MARKET) & (Order.side == exit_side) & Order.stop_price.is_not(None),
                    (Order.side == entry_side) & Order.reduce_only.is_(False) & Order.stop_loss.is_not(None),
                ),
            )
            .order_by(Order.created_at.desc())
            .limit(1)
        )  # fmt: skip
        if order is None:
            return None, "none"
        value = order.stop_price if order.type == OrderType.STOP_MARKET else order.stop_loss
        return Decimal(value), "order_history"

    async def emergency_stop(self, symbol: str, qty: Decimal, mark: Decimal, liquidation: Decimal) -> Decimal | None:
        """Stop at ATR x multiplier (or a percentage) from the mark, kept safely before the liquidation price."""
        if mark <= 0:
            return None
        distance = None
        if settings.PROTECTION_EMERGENCY_STOP_MODE == "atr":
            distance = await self._atr_distance(symbol)
        if distance is None or distance <= 0:
            distance = mark * Decimal(str(settings.PROTECTION_EMERGENCY_STOP_PCT)) / Decimal(100)
        if liquidation > 0 and (liquidation < mark if qty > 0 else liquidation > mark):
            room = abs(mark - liquidation) * Decimal(str(settings.RISK_STOP_MAX_LIQ_DISTANCE_RATIO))
            distance = min(distance, room)
        stop = mark - distance if qty > 0 else mark + distance
        return stop if stop > 0 else None

    async def _atr_distance(self, symbol: str) -> Decimal | None:
        from app.strategy.indicators import atr

        period = settings.PROTECTION_EMERGENCY_ATR_PERIOD
        try:
            klines = await self.client.get_klines(
                symbol, settings.PROTECTION_EMERGENCY_ATR_INTERVAL, limit=max(period * 3, period + 1)
            )
            highs = [float(k[2]) for k in klines]
            lows = [float(k[3]) for k in klines]
            closes = [float(k[4]) for k in klines]
            values = [v for v in atr(highs, lows, closes, period) if v is not None]
        except Exception as exc:
            logger.warning("ATR unavailable for %s (emergency stop falls back to percent): %s", symbol, exc)
            return None
        if not values:
            return None
        return Decimal(str(values[-1])) * Decimal(str(settings.PROTECTION_EMERGENCY_ATR_MULTIPLIER))

    async def _close_crossed(self, summary: GuardSummary, p: dict, pos: Position, stop: Decimal, mark: Decimal) -> None:
        """The stop level was already passed: exit like the stop would have (reduce-only MARKET via OrderService)."""
        from app.services.execution import LiveExecutor, OrderService
        from app.services.risk_manager import RiskManager
        from app.strategy.base import OrderIntent

        symbol, qty = p["symbol"], _dec(p.get("positionAmt"))
        if not settings.LIVE_TRADING_ENABLED:
            await self._unprotected(summary, p, "stop já ultrapassado e LIVE_TRADING_ENABLED=false", stop=stop)
            return
        try:
            filters = await self.client.get_symbol_filters(symbol)
        except Exception:
            filters = None
        equity = Decimal(self.account.last_equity or 0)
        intent = OrderIntent(
            side=OrderSide.SELL if qty > 0 else OrderSide.BUY, quantity=abs(qty), reduce_only=True,
            tag="guard_stop_crossed",
        )  # fmt: skip
        res = await OrderService(LiveExecutor(self.client)).submit(
            self.db, self.account, intent, symbol=symbol, ref_price=mark, leverage=int(pos.leverage or 1),
            risk_state=RiskManager.from_account(self.account, equity), filters=filters,
        )  # fmt: skip
        if not res.ok:
            await self._unprotected(
                summary, p, f"stop {stop} já ultrapassado e o fechamento falhou: {res.reason}"[:200], stop=stop
            )
            return
        summary.closed += 1
        await log_event(
            self.db, "position.stop_loss", level="warning", workspace_id=self.account.workspace_id,
            account_id=self.account.id, symbol=symbol, side="LONG" if qty > 0 else "SHORT", quantity=abs(qty),
            price=mark, reason=f"stop {stop} sem ordem na corretora e já ultrapassado: posição fechada pelo guardião",
        )  # fmt: skip

    async def _unprotected(
        self, summary: GuardSummary, p: dict, reason: str, *, level: str = "critical", external: bool = False,
        stop: Decimal | None = None,
    ) -> None:  # fmt: skip
        summary.unprotected += 1
        qty = _dec(p.get("positionAmt"))
        logger.critical("UNPROTECTED POSITION account=%s %s %s: %s", self.account.id, p.get("symbol"), qty, reason)
        await log_event(
            self.db, "position.unprotected", level=level, workspace_id=self.account.workspace_id,
            account_id=self.account.id, symbol=p.get("symbol"), side="LONG" if qty > 0 else "SHORT",
            quantity=abs(qty), price=_dec(p.get("markPrice")) or None, stop_loss=stop, reason=reason,
            external=external,
        )  # fmt: skip
