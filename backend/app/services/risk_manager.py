"""Deterministic, side-effect-free risk rules.

Every order MUST pass `RiskManager.validate_order` before being sent to the exchange.
Rules (evaluated in this order):
  1. Drawdown kill switch  - equity fell more than `max_drawdown_pct` from peak -> block everything.
  2. Max daily loss        - loss since start of day >= `max_daily_loss_pct` -> block new orders.
  3. Mandatory stop loss   - every opening order needs a stop loss on the correct side of entry.
  4. Leverage limit        - leverage <= `max_leverage`.
  5. Max risk per trade    - qty * |entry - stop| <= equity * `max_risk_per_trade_pct` / 100.
  6. Margin                - notional / leverage <= equity.
Reduce-only orders (closing/de-risking) are always allowed, so positions can be closed
even when the kill switch is active.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"  # long
    SELL = "SELL"  # short


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    quantity: Decimal
    entry_price: Decimal
    stop_loss: Decimal | None
    leverage: int
    reduce_only: bool = False


@dataclass(frozen=True)
class AccountRiskState:
    equity: Decimal  # current equity (wallet + unrealized PnL), USDT
    peak_equity: Decimal  # highest equity observed (for drawdown)
    day_start_equity: Decimal  # equity at 00:00 UTC
    max_risk_per_trade_pct: Decimal = Decimal("1")
    max_leverage: int = 5
    max_daily_loss_pct: Decimal = Decimal("3")
    max_drawdown_pct: Decimal = Decimal("15")


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    kill_switch: bool = False

    def __iter__(self):  # allows `ok, reason = rm.validate_order(...)`
        yield self.allowed
        yield self.reason


class RiskManager:
    OK = "ok"

    @staticmethod
    def _pct(part: Decimal, whole: Decimal) -> Decimal:
        return (part / whole * Decimal(100)) if whole > 0 else Decimal(0)

    def drawdown_pct(self, account: AccountRiskState) -> Decimal:
        return self._pct(account.peak_equity - account.equity, account.peak_equity)

    def daily_loss_pct(self, account: AccountRiskState) -> Decimal:
        return self._pct(account.day_start_equity - account.equity, account.day_start_equity)

    def is_kill_switch_triggered(self, account: AccountRiskState) -> bool:
        return self.drawdown_pct(account) >= account.max_drawdown_pct

    def trade_risk(self, order: OrderRequest) -> Decimal:
        """Loss in quote currency (USDT) if the stop loss is hit."""
        if order.stop_loss is None:
            return Decimal(0)
        return order.quantity * abs(order.entry_price - order.stop_loss)

    def evaluate(self, order: OrderRequest, account: AccountRiskState) -> RiskDecision:
        if order.quantity <= 0 or order.entry_price <= 0:
            return RiskDecision(False, "invalid_order: quantity and entry_price must be positive")

        if order.reduce_only:
            return RiskDecision(True, self.OK)

        if account.equity <= 0:
            return RiskDecision(False, "no_equity: account equity must be positive")

        if self.is_kill_switch_triggered(account):
            return RiskDecision(
                False,
                f"kill_switch: drawdown {self.drawdown_pct(account):.2f}% >= {account.max_drawdown_pct}%",
                kill_switch=True,
            )

        if self.daily_loss_pct(account) >= account.max_daily_loss_pct:
            return RiskDecision(
                False, f"max_daily_loss: {self.daily_loss_pct(account):.2f}% >= {account.max_daily_loss_pct}%"
            )

        if order.stop_loss is None or order.stop_loss <= 0:
            return RiskDecision(False, "stop_loss_required: every opening order needs a stop loss")
        if order.side == Side.BUY and order.stop_loss >= order.entry_price:
            return RiskDecision(False, "stop_loss_invalid: long stop must be below entry price")
        if order.side == Side.SELL and order.stop_loss <= order.entry_price:
            return RiskDecision(False, "stop_loss_invalid: short stop must be above entry price")

        if order.leverage < 1 or order.leverage > account.max_leverage:
            return RiskDecision(False, f"leverage_limit: {order.leverage}x outside 1..{account.max_leverage}x")

        risk = self.trade_risk(order)
        max_risk = account.equity * account.max_risk_per_trade_pct / Decimal(100)
        if risk > max_risk:
            return RiskDecision(False, f"max_risk_per_trade: risk {risk:.2f} > allowed {max_risk:.2f} USDT")

        margin = order.quantity * order.entry_price / Decimal(order.leverage)
        if margin > account.equity:
            return RiskDecision(False, f"insufficient_margin: required {margin:.2f} > equity {account.equity:.2f} USDT")

        return RiskDecision(True, self.OK)

    @classmethod
    def from_account(cls, account, equity: Decimal) -> AccountRiskState:
        """Build the risk state from an Account ORM row and its current equity."""
        return AccountRiskState(
            equity=equity,
            peak_equity=max(Decimal(account.peak_equity or equity), equity),
            day_start_equity=Decimal(account.day_start_equity or equity),
            max_risk_per_trade_pct=Decimal(account.max_risk_per_trade_pct),
            max_leverage=int(account.max_leverage),
            max_daily_loss_pct=Decimal(account.max_daily_loss_pct),
            max_drawdown_pct=Decimal(account.max_drawdown_pct),
        )

    def validate_order(self, order: OrderRequest, account: AccountRiskState) -> tuple[bool, str]:
        decision = self.evaluate(order, account)
        return decision.allowed, decision.reason

    def max_position_size(self, entry_price: Decimal, stop_loss: Decimal, account: AccountRiskState) -> Decimal:
        """Largest quantity that respects the per-trade risk limit."""
        distance = abs(entry_price - stop_loss)
        if distance == 0:
            return Decimal(0)
        return account.equity * account.max_risk_per_trade_pct / Decimal(100) / distance
