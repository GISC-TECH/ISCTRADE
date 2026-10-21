"""Exchange symbol filters (tick size, step size, minimums) and Decimal rounding helpers."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any


def _round_step(value: Decimal, step: Decimal, rounding: str = ROUND_DOWN) -> Decimal:
    if step <= 0:
        return value
    step = step.normalize()
    units = (value / step).to_integral_value(rounding=rounding)
    result = units * step
    return result.quantize(step) if step.as_tuple().exponent < 0 else result


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal = Decimal("0.01")
    step_size: Decimal = Decimal("0.001")
    min_qty: Decimal = Decimal("0.001")
    max_qty: Decimal = Decimal("1000000")
    market_step_size: Decimal | None = None
    market_max_qty: Decimal | None = None
    min_notional: Decimal = Decimal("5")

    def round_price(self, price: Decimal, rounding: str = ROUND_HALF_UP) -> Decimal:
        return _round_step(Decimal(price), self.tick_size, rounding)

    def round_qty(self, qty: Decimal, market: bool = True) -> Decimal:
        step = (self.market_step_size if market else None) or self.step_size
        return _round_step(Decimal(qty), step, ROUND_DOWN)

    def check(self, qty: Decimal, price: Decimal, reduce_only: bool = False, market: bool = True) -> str | None:
        """Return a rejection reason or None if the order satisfies the filters.

        MARKET orders are bounded by MARKET_LOT_SIZE, LIMIT orders by LOT_SIZE (usually much larger).
        """
        if qty < self.min_qty:
            return f"filter_min_qty: {qty} < {self.min_qty}"
        max_qty = (self.market_max_qty if market else None) or self.max_qty
        if qty > max_qty:
            return f"filter_max_qty: {qty} > {max_qty}"
        if not reduce_only and qty * price < self.min_notional:
            return f"filter_min_notional: {qty * price:.4f} < {self.min_notional}"
        return None

    @classmethod
    def from_exchange_info(cls, symbol_info: dict[str, Any]) -> SymbolFilters:
        f = {x["filterType"]: x for x in symbol_info.get("filters", [])}
        price = f.get("PRICE_FILTER", {})
        lot = f.get("LOT_SIZE", {})
        mlot = f.get("MARKET_LOT_SIZE", {})
        notional = f.get("MIN_NOTIONAL", {})
        return cls(
            symbol=symbol_info["symbol"],
            tick_size=Decimal(price.get("tickSize", "0.01")),
            step_size=Decimal(lot.get("stepSize", "0.001")),
            min_qty=Decimal(lot.get("minQty", "0.001")),
            max_qty=Decimal(lot.get("maxQty", "1000000")),
            market_step_size=Decimal(mlot["stepSize"]) if mlot.get("stepSize") else None,
            market_max_qty=Decimal(mlot["maxQty"]) if mlot.get("maxQty") else None,
            min_notional=Decimal(notional.get("notional", notional.get("minNotional", "5"))),
        )


def default_filters(symbol: str) -> SymbolFilters:
    return SymbolFilters(symbol=symbol)
