"""Audit fixes: aggregate margin over open positions, LOT_SIZE vs MARKET_LOT_SIZE for LIMIT orders."""

from decimal import Decimal as D

from app.models import ExecutionMode, OrderSide, OrderType, Position
from app.services.execution import OrderService, PaperExecutor
from app.services.risk_manager import AccountRiskState, OrderRequest, RiskManager, Side
from app.services.symbol_filters import SymbolFilters
from app.strategy.base import OrderIntent
from tests.factories import make_account, make_user_workspace


def risk(equity="1000") -> AccountRiskState:
    e = D(equity)
    return AccountRiskState(equity=e, peak_equity=e, day_start_equity=e, max_leverage=5, max_risk_per_trade_pct=D("5"))


def test_margin_includes_used_margin() -> None:
    order = OrderRequest("BTCUSDT", Side.BUY, D("1"), D("100"), D("99"), leverage=1)
    assert RiskManager().evaluate(order, risk("150")).allowed
    from dataclasses import replace

    decision = RiskManager().evaluate(order, replace(risk("150"), used_margin=D("60")))
    assert not decision.allowed and decision.reason.startswith("insufficient_margin")


async def _account_with_position(db, symbol: str, qty: str):
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.PAPER)
    db.add(Position(workspace_id=ws.id, account_id=acc.id, symbol=symbol, quantity=D(qty), entry_price=D("100"),
                    mark_price=D("100"), leverage=1, is_paper=True))  # fmt: skip
    await db.flush()
    return acc


async def test_order_service_counts_other_positions_margin(db) -> None:
    acc = await _account_with_position(db, "ETHUSDT", "9")  # 900 USDT of margin already in use
    res = await OrderService(PaperExecutor()).submit(
        db, acc, OrderIntent(OrderSide.BUY, D("2"), stop_loss=D("99")), symbol="BTCUSDT",
        ref_price=D("100"), leverage=1, risk_state=risk(),
    )  # fmt: skip
    assert not res.ok and "insufficient_margin" in res.reason


async def test_position_being_reduced_is_not_counted(db) -> None:
    acc = await _account_with_position(db, "BTCUSDT", "9")  # long BTC; a SELL on BTC reduces it
    res = await OrderService(PaperExecutor()).submit(
        db, acc, OrderIntent(OrderSide.SELL, D("2"), stop_loss=D("101")), symbol="BTCUSDT",
        ref_price=D("100"), leverage=1, risk_state=risk(),
    )  # fmt: skip
    assert "insufficient_margin" not in (res.reason or "")


def test_limit_orders_use_lot_size_max() -> None:
    f = SymbolFilters(symbol="BTCUSDT", max_qty=D("1000"), market_max_qty=D("120"))
    assert f.check(D("500"), D("50000")).startswith("filter_max_qty")
    assert f.check(D("500"), D("50000"), market=False) is None


async def test_order_service_limit_not_bounded_by_market_lot(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.PAPER)
    f = SymbolFilters(symbol="BTCUSDT", max_qty=D("1000"), market_max_qty=D("1"), step_size=D("0.001"),
                      min_qty=D("0.001"))  # fmt: skip
    res = await OrderService(PaperExecutor()).submit(
        db, acc, OrderIntent(OrderSide.BUY, D("2"), stop_loss=D("99")), symbol="BTCUSDT", ref_price=D("100"),
        leverage=1, risk_state=risk("100000"), filters=f, order_type=OrderType.LIMIT, limit_price=D("100"),
    )  # fmt: skip
    assert "filter_max_qty" not in (res.reason or "")
