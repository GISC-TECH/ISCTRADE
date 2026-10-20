from decimal import Decimal as D

import pytest

from app.models.enums import OrderSide
from app.services.position_math import apply_fill, stop_triggered, unrealized_pnl
from app.services.symbol_filters import SymbolFilters


def test_open_add_reduce_close_long() -> None:
    o = apply_fill(D(0), D(0), OrderSide.BUY, D(1), D(100))
    assert (o.quantity, o.entry_price, o.realized_pnl) == (D(1), D(100), D(0))
    o = apply_fill(o.quantity, o.entry_price, OrderSide.BUY, D(1), D(110))
    assert o.entry_price == D(105)
    o = apply_fill(o.quantity, o.entry_price, OrderSide.SELL, D("0.5"), D(115))
    assert o.realized_pnl == D(5) and o.quantity == D("1.5") and o.entry_price == D(105)
    o = apply_fill(o.quantity, o.entry_price, OrderSide.SELL, D("1.5"), D(100))
    assert o.realized_pnl == D("-7.5") and o.quantity == 0 and o.entry_price == 0


def test_short_and_flip() -> None:
    o = apply_fill(D(0), D(0), OrderSide.SELL, D(2), D(100))
    assert o.quantity == D(-2)
    o = apply_fill(o.quantity, o.entry_price, OrderSide.BUY, D(3), D(90))
    assert o.realized_pnl == D(20) and o.quantity == D(1) and o.entry_price == D(90)


def test_unrealized_and_stop() -> None:
    assert unrealized_pnl(D(-2), D(100), D(95)) == D(10)
    assert stop_triggered(D(1), D(95), D(94), D(101))
    assert not stop_triggered(D(1), D(95), D(96), D(101))
    assert stop_triggered(D(-1), D(105), D(99), D(106))
    assert not stop_triggered(D(1), None, D(1), D(2))


def test_invalid_fill() -> None:
    with pytest.raises(ValueError):
        apply_fill(D(0), D(0), OrderSide.BUY, D(0), D(1))


def test_symbol_filters_rounding_and_checks() -> None:
    info = {
        "symbol": "BTCUSDT",
        "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
            {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "maxQty": "120"},
            {"filterType": "MIN_NOTIONAL", "notional": "100"},
        ],
    }
    f = SymbolFilters.from_exchange_info(info)
    assert f.round_qty(D("0.12345")) == D("0.123")
    assert f.round_price(D("64250.06")) == D("64250.1")
    assert f.check(D("0.0005"), D(60000)).startswith("filter_min_qty")
    assert f.check(D("0.001"), D(60000)).startswith("filter_min_notional")
    assert f.check(D("0.001"), D(60000), reduce_only=True) is None
    assert f.check(D("121"), D(1)).startswith("filter_max_qty")
    assert f.check(D("0.01"), D(60000)) is None
