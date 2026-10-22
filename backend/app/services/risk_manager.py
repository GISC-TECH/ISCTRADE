"""Deterministic, side-effect-free risk rules.

Every order MUST pass `RiskManager.validate_order` before being sent to the exchange.
Rules (evaluated in this order):
  1. Drawdown kill switch  - equity fell more than `max_drawdown_pct` from peak -> block everything.
  2. Max daily loss        - loss since start of day >= `max_daily_loss_pct` -> block new orders.
  3. Mandatory stop loss   - every opening order needs a stop loss on the correct side of entry.
  4. Leverage limit        - leverage <= min(`max_leverage`, RISK_MAX_LEVERAGE_CAP) and the exchange bracket.
  4b. Liquidation          - the stop must trigger safely before the estimated (isolated) liquidation price.
  5. Max risk per trade    - qty * |entry - stop| <= equity * `max_risk_per_trade_pct` / 100.
  6. Margin                - notional / leverage + margin already used by open positions <= equity.
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
    # Maintenance margin of the exchange bracket for this notional (None = liquidation check skipped).
    maint_margin_rate: Decimal | None = None
    maint_amount: Decimal = Decimal("0")  # Binance "cum" of the bracket
    bracket_max_leverage: int | None = None  # highest leverage the exchange allows for this notional


@dataclass(frozen=True)
class AccountRiskState:
    equity: Decimal  # current equity (wallet + unrealized PnL), USDT
    peak_equity: Decimal  # highest equity observed (for drawdown)
    day_start_equity: Decimal  # equity at 00:00 UTC
    max_risk_per_trade_pct: Decimal = Decimal("1")
    max_leverage: int = 5
    max_daily_loss_pct: Decimal = Decimal("3")
    max_drawdown_pct: Decimal = Decimal("15")
    used_margin: Decimal = Decimal("0")  # initial margin of the open positions (notional / leverage)


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str
    kill_switch: bool = False

    def __iter__(self):  # allows `ok, reason = rm.validate_order(...)`
        yield self.allowed
        yield self.reason


def estimate_liquidation_price(
    side: Side,
    entry: Decimal,
    leverage: int,
    maint_margin_rate: Decimal,
    maint_amount: Decimal = Decimal("0"),
    quantity: Decimal | None = None,
) -> Decimal:
    """Isolated-margin liquidation price of a fresh one-way position (Binance formula, no extra margin).

    Long:  LP = entry * (1 - 1/L) / (1 - mmr) - cum / (qty * (1 - mmr))
    Short: LP = entry * (1 + 1/L) / (1 + mmr) + cum / (qty * (1 + mmr))
    Cross margin liquidates further away (the whole wallet backs the position), so this is conservative.
    """
    lev = Decimal(max(int(leverage), 1))
    cum_term = maint_amount / quantity if quantity and quantity > 0 else Decimal("0")
    if side == Side.BUY:
        return max((entry * (1 - 1 / lev) - cum_term) / (1 - maint_margin_rate), Decimal("0"))
    return (entry * (1 + 1 / lev) + cum_term) / (1 + maint_margin_rate)


class RiskManager:
    OK = "ok"

    def __init__(self, leverage_cap: int | None = None, liq_distance_ratio: Decimal | None = None) -> None:
        from app.core.config import settings

        self.leverage_cap = int(leverage_cap if leverage_cap is not None else settings.RISK_MAX_LEVERAGE_CAP)
        self.liq_distance_ratio = Decimal(
            str(liq_distance_ratio if liq_distance_ratio is not None else settings.RISK_STOP_MAX_LIQ_DISTANCE_RATIO)
        )

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

        max_lev = min(account.max_leverage, self.leverage_cap)
        if order.leverage < 1 or order.leverage > max_lev:
            return RiskDecision(False, f"leverage_limit: {order.leverage}x outside 1..{max_lev}x")
        if order.bracket_max_leverage is not None and order.leverage > order.bracket_max_leverage:
            return RiskDecision(
                False, f"leverage_bracket: {order.leverage}x above the exchange limit {order.bracket_max_leverage}x"
                " for this notional",
            )  # fmt: skip

        if order.maint_margin_rate is not None:
            liq = estimate_liquidation_price(
                order.side, order.entry_price, order.leverage, order.maint_margin_rate, order.maint_amount,
                order.quantity,
            )  # fmt: skip
            liq_distance = abs(order.entry_price - liq)
            stop_distance = abs(order.entry_price - order.stop_loss)
            if liq_distance <= 0 or stop_distance > liq_distance * self.liq_distance_ratio:
                return RiskDecision(
                    False,
                    f"stop_beyond_liquidation: stop {order.stop_loss} too close to/after the estimated liquidation"
                    f" price {liq:.8f} ({order.leverage}x); lower the leverage or tighten the stop",
                )

        risk = self.trade_risk(order)
        max_risk = account.equity * account.max_risk_per_trade_pct / Decimal(100)
        if risk > max_risk:
            return RiskDecision(False, f"max_risk_per_trade: risk {risk:.2f} > allowed {max_risk:.2f} USDT")

        margin = order.quantity * order.entry_price / Decimal(order.leverage)
        if margin + account.used_margin > account.equity:
            return RiskDecision(
                False,
                f"insufficient_margin: required {margin:.2f} + in use {account.used_margin:.2f}"
                f" > equity {account.equity:.2f} USDT",
            )

        return RiskDecision(True, self.OK)

    @classmethod
    def from_account(cls, account, equity: Decimal) -> AccountRiskState:
        """Build the risk state from an Account ORM row and its current equity."""
        return AccountRiskState(
            equity=equity,
            peak_equity=max(Decimal(account.peak_equity or equity), equity),
            day_start_equity=Decimal(account.day_start_equity or equity),
            max_risk_per_trade_pct=Decimal(account.max_risk_per_trade_pct),
            max_leverage=int(account.max_leverage),  # clamped to RISK_MAX_LEVERAGE_CAP in evaluate()
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
