"""Paper realism (G-10a/F-13): volume-capped partial fills, LIMIT trade-through and gap fills for stops."""

from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.backtest.engine import BacktestConfig, run_backtest
from app.core.config import settings
from app.models import Order, OrderSide, OrderStatus, OrderType
from app.models.audit import AuditLog
from app.models.trading import Position
from app.services import paper_orders
from app.services.bot_runner import run_bot_tick
from app.services.execution import PaperExecutor, gap_stop_price
from app.services.market_data import StaticMarketData
from app.strategy.base import Candle
from tests.factories import HOUR_MS, candles_from_closes, make_account, make_bot, make_user_workspace
from tests.test_backtest_funding_wf import CFG, HoldStrategy, flat

BREAKOUT = [100 + (1 if i % 2 else -1) for i in range(30)] + [106.0]
DONCHIAN = {"entry_period": 10, "exit_period": 5, "atr_period": 5, "allow_short": False}


def test_gap_stop_price() -> None:
    slip = D("0.001")
    assert gap_stop_price(D(1), D(95), D(97), slip) == D(95) * D("0.999")  # touched intrabar: stop
    assert gap_stop_price(D(1), D(95), D(90), slip) == D(90) * D("0.999")  # gapped through: open (worse)
    assert gap_stop_price(D(-1), D(105), D(110), slip) == D(110) * D("1.001")
    assert gap_stop_price(D(-1), D(105), D(101), slip) == D(105) * D("1.001")


def test_limit_trade_through() -> None:
    assert paper_orders.limit_crossed(OrderSide.BUY, D(100), D(100))
    assert not paper_orders.limit_crossed(OrderSide.BUY, D(100), D(100), D(10))
    assert paper_orders.limit_crossed(OrderSide.BUY, D(100), D("99.9"), D(10))
    assert not paper_orders.limit_crossed(OrderSide.SELL, D(100), D("100.05"), D(10))
    assert paper_orders.limit_crossed(OrderSide.SELL, D(100), D("100.1"), D(10))


async def _open_breakout(db, email: str):
    _, ws = await make_user_workspace(db, email=email)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "donchian_breakout", DONCHIAN, symbol="AAAUSDT")
    await db.commit()
    candles = candles_from_closes(BREAKOUT)
    market = StaticMarketData({"AAAUSDT": candles})
    res = await run_bot_tick(db, bot, acc, market, executor=PaperExecutor())
    pos = await db.scalar(select(Position).where(Position.account_id == acc.id, Position.symbol == "AAAUSDT"))
    return acc, bot, market, candles, res, pos


async def test_paper_stop_gap_fills_at_open(db) -> None:
    acc, bot, market, candles, res, pos = await _open_breakout(db, "gap@example.com")
    assert res.filled == 1 and pos.quantity > 0 and pos.stop_loss is not None
    stop = D(pos.stop_loss)
    last = candles[-1]
    gap_open = float(stop) * 0.95  # overnight-style gap well below the stop
    market.candles["AAAUSDT"] = [
        *candles,
        Candle(last.open_time + HOUR_MS, gap_open, gap_open * 1.001, gap_open * 0.99, gap_open, 100.0,
               last.close_time + HOUR_MS),
    ]  # fmt: skip
    ex = PaperExecutor()
    await run_bot_tick(db, bot, acc, market, executor=ex)
    await db.refresh(pos)
    assert pos.quantity == 0
    order = await db.scalar(select(Order).where(Order.account_id == acc.id, Order.reason == "stop_loss"))
    expected = D(str(gap_open)) * (1 - ex.slippage)
    assert order.avg_price == pytest.approx(expected) and order.avg_price < stop


async def test_bot_entry_partial_fill_capped_by_volume(db, monkeypatch) -> None:
    acc, *_, base = await _open_breakout(db, "full@example.com")
    full_qty = base.quantity
    monkeypatch.setattr(settings, "PAPER_MAX_VOLUME_PCT", 1.0)  # candles have volume 100 -> cap 1 unit
    acc, _, _, _, res, pos = await _open_breakout(db, "partial@example.com")
    assert full_qty > 1
    assert res.filled == 1 and pos.quantity == D(1)
    [order] = list(await db.scalars(select(Order).where(Order.account_id == acc.id)))
    assert order.status == OrderStatus.EXPIRED and order.executed_qty == D(1) and order.quantity > D(1)
    audit = await db.scalar(select(AuditLog).where(AuditLog.event == "order.partial_fill"))
    assert audit is not None and audit.data["remainder"] == "expired"


async def test_paper_executor_never_caps_exits(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PAPER_MAX_VOLUME_PCT", 1.0)
    acc, *_ = await _open_breakout(db, "exit@example.com")
    ex = PaperExecutor()
    ex.liquidity["AAAUSDT"] = D(1)  # cap 0.01
    res = await ex.execute(db, acc, None, "AAAUSDT", OrderSide.SELL, D(1), D(100), stop_loss=None,
                           reduce_only=True, leverage=1, tag="close")  # fmt: skip
    assert res.ok and res.quantity == D(1)
    res = await ex.execute(db, acc, None, "AAAUSDT", OrderSide.BUY, D(5), D(100), stop_loss=D(90),
                           reduce_only=False, leverage=1, tag="enter")  # fmt: skip
    assert res.ok and res.quantity == D("0.01")


async def test_resting_limit_partially_fills_over_checks(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db, email="limit@example.com")
    acc = await make_account(db, ws)
    await db.commit()
    prices = {"BTCUSDT": D("50000")}
    vol_calls: list[str] = []

    async def price(symbol, testnet):
        return prices.get(symbol)

    async def volume(symbol, testnet):
        vol_calls.append(symbol)
        return D("0.5")  # last 1m candle volume -> 10% = 0.05 per check

    monkeypatch.setattr(paper_orders, "price_source", price)
    monkeypatch.setattr(paper_orders, "volume_source", volume)
    monkeypatch.setattr(settings, "PAPER_MAX_VOLUME_PCT", 10.0)
    monkeypatch.setattr(settings, "PAPER_LIMIT_THROUGH_BPS", 5.0)
    res = await PaperExecutor().execute(
        db, acc, None, "BTCUSDT", OrderSide.BUY, D("0.12"), D("50000"), stop_loss=D("48000"), reduce_only=False,
        leverage=1, tag="limit", order_type=OrderType.LIMIT, limit_price=D("49500"),
    )  # fmt: skip
    assert res.reason == "resting"
    await db.commit()
    order = res.order

    prices["BTCUSDT"] = D("49490")  # touched but not 5 bps through -> no fill
    assert (await paper_orders.process_paper_orders(db)).filled == 0 and not vol_calls
    prices["BTCUSDT"] = D("49400")
    report = await paper_orders.process_paper_orders(db)
    await db.refresh(order)
    assert report.partial == 1 and order.status == OrderStatus.PARTIALLY_FILLED and order.executed_qty == D("0.05")
    report = await paper_orders.process_paper_orders(db)
    await db.refresh(order)
    assert report.partial == 1 and order.executed_qty == D("0.10")
    report = await paper_orders.process_paper_orders(db)
    await db.refresh(order)
    assert report.filled == 1 and order.status == OrderStatus.FILLED and order.executed_qty == D("0.12")
    assert order.quantity == D("0.12") and order.avg_price == D("49500")
    # regression: a LIMIT fill adding to a position that already has a stop used to be discarded by refresh()
    pos = await db.scalar(select(Position).where(Position.account_id == acc.id, Position.symbol == "BTCUSDT"))
    await db.refresh(pos)
    assert pos.quantity == D("0.12") and pos.stop_loss == D("48000")
    audit = await db.scalar(select(AuditLog).where(AuditLog.event == "order.partial_fill"))
    assert audit is not None and audit.data["remainder"] == "resting"


def test_backtest_volume_cap() -> None:
    candles = flat(10)  # volume 100 per candle
    full = run_backtest(HoldStrategy(OrderSide.BUY, qty="10"), candles, "BTCUSDT", "1h", CFG)
    assert full.trades[0].quantity == D(10) and full.metrics["partial_fills"] == 0
    cfg = BacktestConfig(**{**CFG.__dict__, "max_volume_pct": D(5)})
    capped = run_backtest(HoldStrategy(OrderSide.BUY, qty="10"), candles, "BTCUSDT", "1h", cfg)
    assert capped.trades[0].quantity == D(5) and capped.metrics["partial_fills"] == 1
    assert BacktestConfig.from_dict({"max_volume_pct": 2.5}).max_volume_pct == D("2.5")
