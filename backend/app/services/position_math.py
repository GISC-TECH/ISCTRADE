"""Pure position accounting (one-way mode), shared by paper trading and the backtest engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.models.enums import OrderSide

ZERO = Decimal("0")


@dataclass(frozen=True)
class FillOutcome:
    quantity: Decimal  # new signed position size
    entry_price: Decimal  # new average entry (0 when flat)
    realized_pnl: Decimal  # PnL realized by this fill (before fees)
    closed_qty: Decimal  # portion of the fill that reduced the existing position


def apply_fill(
    position_qty: Decimal, entry_price: Decimal, side: OrderSide, fill_qty: Decimal, fill_price: Decimal
) -> FillOutcome:
    if fill_qty <= 0:
        raise ValueError("fill_qty must be positive")
    delta = fill_qty if side == OrderSide.BUY else -fill_qty
    if position_qty == 0 or (position_qty > 0) == (delta > 0):
        size = abs(position_qty) + fill_qty
        entry = (abs(position_qty) * entry_price + fill_qty * fill_price) / size
        return FillOutcome(position_qty + delta, entry, ZERO, ZERO)

    closed = min(abs(position_qty), fill_qty)
    direction = Decimal(1) if position_qty > 0 else Decimal(-1)
    realized = closed * (fill_price - entry_price) * direction
    new_qty = position_qty + delta
    if new_qty == 0:
        new_entry = ZERO
    elif (new_qty > 0) != (position_qty > 0):
        new_entry = fill_price  # position flipped
    else:
        new_entry = entry_price
    return FillOutcome(new_qty, new_entry, realized, closed)


def unrealized_pnl(position_qty: Decimal, entry_price: Decimal, mark_price: Decimal) -> Decimal:
    if position_qty == 0:
        return ZERO
    return position_qty * (mark_price - entry_price)


def stop_triggered(position_qty: Decimal, stop_loss: Decimal | None, low: Decimal, high: Decimal) -> bool:
    if stop_loss is None or position_qty == 0:
        return False
    return low <= stop_loss if position_qty > 0 else high >= stop_loss
