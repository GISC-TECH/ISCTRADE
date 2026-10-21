from decimal import Decimal as D

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import settings
from app.models import (
    AccountStatus,
    AuditLog,
    BotState,
    ExecutionMode,
    Order,
    OrderSide,
    OrderStatus,
    Position,
    Trade,
)
from app.services.binance_client import BinanceFuturesClient
from app.services.bot_runner import run_bot_tick
from app.services.execution import (
    LiveExecutor,
    LiveTradingDisabled,
    OrderService,
    PaperExecutor,
    build_executor,
)
from app.services.grid_bot import GridBot
from app.services.kill_switch import set_global_kill_switch
from app.services.market_data import StaticMarketData
from app.services.risk_manager import AccountRiskState, RiskManager
from app.strategy.base import OrderIntent
from tests.factories import candles_from_closes, make_account, make_bot, make_user_workspace

GRID = {"lower_price": 90, "upper_price": 110, "levels": 5, "quantity_per_level": 0.5, "stop_loss_pct": 5}


def risk(equity="10000") -> AccountRiskState:
    e = D(equity)
    return AccountRiskState(equity=e, peak_equity=e, day_start_equity=e, max_leverage=5)


async def test_paper_executor_fills_and_updates_ledger(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    svc = OrderService(PaperExecutor(fee_rate=D("0.001"), slippage_bps=D(0)))
    res = await svc.submit(
        db, acc, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("95")), symbol="BTCUSDT",
        ref_price=D("100"), leverage=2, risk_state=risk(),
    )  # fmt: skip
    assert res.ok and res.fill_price == D(100)
    res = await svc.submit(
        db, acc, OrderIntent(OrderSide.SELL, D("1"), reduce_only=True), symbol="BTCUSDT",
        ref_price=D("110"), leverage=2, risk_state=risk(),
    )  # fmt: skip
    assert res.ok and res.realized_pnl == D(10)
    await db.commit()
    # 10 profit - fees (0.1 + 0.11)
    assert acc.paper_balance == D("10000") + D(10) - D("0.21")
    pos = await db.scalar(select(Position))
    assert pos.quantity == 0 and pos.stop_loss is None
    assert await db.scalar(select(func.count()).select_from(Trade)) == 2


async def test_risk_rejection_is_recorded(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    svc = OrderService(PaperExecutor())
    res = await svc.submit(
        db, acc, OrderIntent(OrderSide.BUY, D("1")), symbol="BTCUSDT", ref_price=D("100"), leverage=1,
        risk_state=risk(),
    )  # fmt: skip
    assert not res.ok and res.reason.startswith("stop_loss_required")
    await db.commit()
    order = await db.scalar(select(Order))
    assert order.status == OrderStatus.REJECTED
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event == "order.rejected")) == 1


async def test_auto_sizing_respects_risk(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    svc = OrderService(PaperExecutor(slippage_bps=D(0)))
    # 1% of 10k = 100 USDT risk, stop distance 5 -> 20 units; margin 2000/5 ok
    res = await svc.submit(
        db, acc, OrderIntent(OrderSide.BUY, None, stop_loss=D("95"), risk_fraction=D("0.5")), symbol="BTCUSDT",
        ref_price=D("100"), leverage=5, risk_state=risk(),
    )  # fmt: skip
    assert res.ok and res.quantity == D("10")


async def test_reduce_only_without_position_rejected(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    res = await OrderService(PaperExecutor()).submit(
        db, acc, OrderIntent(OrderSide.SELL, D("1"), reduce_only=True), symbol="BTCUSDT",
        ref_price=D("100"), leverage=1, risk_state=risk(),
    )  # fmt: skip
    assert not res.ok and res.reason == "reduce_only_no_position"


async def test_global_kill_switch_blocks_new_orders(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    await set_global_kill_switch(db, True, None, "test")
    res = await OrderService(PaperExecutor()).submit(
        db, acc, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("99")), symbol="BTCUSDT",
        ref_price=D("100"), leverage=1, risk_state=risk(),
    )  # fmt: skip
    assert not res.ok and res.reason == "global_kill_switch"


async def test_live_mode_is_double_gated(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    paper = await make_account(db, ws)
    assert isinstance(build_executor(paper), PaperExecutor)
    live = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", False)
    with pytest.raises(LiveTradingDisabled):
        build_executor(live)
    with pytest.raises(LiveTradingDisabled):
        LiveExecutor(BinanceFuturesClient("k", "s"))


async def test_live_executor_with_mocked_binance(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        q = dict(request.url.params)
        assert "signature" in q and request.headers["X-MBX-APIKEY"] == "k"
        if q["type"] == "MARKET":
            return httpx.Response(
                200, json={"orderId": 1, "status": "FILLED", "executedQty": q["quantity"], "avgPrice": "100.5"}
            )
        assert request.url.path == "/fapi/v1/algoOrder" and q["algoType"] == "CONDITIONAL"
        return httpx.Response(200, json={"algoId": 2, "clientAlgoId": q["clientAlgoId"], "algoStatus": "NEW"})

    client = BinanceFuturesClient("k", "s", testnet=True, transport=httpx.MockTransport(handler))
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    res = await OrderService(LiveExecutor(client)).submit(
        db, acc, OrderIntent(OrderSide.BUY, D("0.5"), stop_loss=D("95")), symbol="BTCUSDT",
        ref_price=D("100"), leverage=2, risk_state=risk(),
    )  # fmt: skip
    await client.close()
    assert res.ok and res.order.exchange_order_id == "1"
    assert [dict(c.url.params)["type"] for c in calls] == ["MARKET", "STOP_MARKET"]
    stop_params = dict(calls[1].url.params)
    # since 2025-12-09 conditional orders must use the Algo Order API (triggerPrice, no quantity/reduceOnly)
    assert stop_params["closePosition"] == "true" and stop_params["triggerPrice"] == "95"
    assert "quantity" not in stop_params and "reduceOnly" not in stop_params
    assert all("testnet" in str(c.url) for c in calls)


async def test_grid_bot_tick_paper_end_to_end(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "grid", GRID)
    await db.commit()
    closes = [101.0]
    market = StaticMarketData({"BTCUSDT": candles_from_closes(closes)})
    runner = GridBot(db, bot, acc, market, executor=PaperExecutor(slippage_bps=D(0)))
    assert (await runner.tick()).filled == 0
    assert (await runner.tick()).message == "no_new_candle"
    # price falls through 100 -> buy level 2
    closes += [99.0]
    market.candles["BTCUSDT"] = candles_from_closes(closes)
    res = await runner.tick()
    assert res.filled == 1, res.reasons
    pos = await db.scalar(select(Position))
    assert pos.quantity == D("0.5") and pos.stop_loss == D("85.50")
    # price rises to 105 -> sell
    closes += [105.5]
    market.candles["BTCUSDT"] = candles_from_closes(closes)
    res = await runner.tick()
    assert res.filled == 1
    await db.commit()
    assert pos.quantity == 0
    assert acc.paper_balance > D("10000") - D("1")


async def test_bot_tick_drawdown_triggers_kill_switch(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, peak_equity=D("20000"))
    bot = await make_bot(db, acc, "grid", GRID)
    await db.commit()
    market = StaticMarketData({"BTCUSDT": candles_from_closes([101.0])})
    res = await run_bot_tick(db, bot, acc, market, executor=PaperExecutor())
    assert res.status == "killed"
    assert acc.status == AccountStatus.KILLED and bot.state == BotState.KILLED
    events = (await db.scalars(select(AuditLog.event))).all()
    assert "risk.kill_switch" in events


async def test_paper_stop_loss_simulated(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "grid", GRID)
    await db.commit()
    closes = [101.0, 99.0]
    market = StaticMarketData({"BTCUSDT": candles_from_closes(closes[:1])})
    runner = GridBot(db, bot, acc, market, executor=PaperExecutor(slippage_bps=D(0)))
    await runner.tick()
    market.candles["BTCUSDT"] = candles_from_closes(closes)
    await runner.tick()
    closes += [80.0]  # low pierces stop at 85.5
    market.candles["BTCUSDT"] = candles_from_closes(closes)
    await runner.tick()
    await db.commit()
    pos = await db.scalar(select(Position))
    assert pos.quantity == 0
    assert bot.runtime["halted"] is True
    stop_trade = (await db.scalars(select(Trade).order_by(Trade.executed_at))).all()[-1]
    assert stop_trade.price == D("85.50") and stop_trade.realized_pnl < 0


async def test_bot_error_state_on_exception(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "grid", {"lower_price": 1})  # invalid params
    await db.commit()
    res = await run_bot_tick(db, bot, acc, StaticMarketData(), executor=PaperExecutor())
    assert res.status == "error"
    assert bot.state == BotState.ERROR and "required" in bot.last_error


def test_risk_manager_margin_rule() -> None:
    from app.services.risk_manager import OrderRequest

    rm = RiskManager()
    ok, reason = rm.validate_order(
        OrderRequest("BTCUSDT", OrderSide.BUY, D("200"), D("100"), D("99.5"), 1), risk("10000")
    )
    assert not ok and reason.startswith("insufficient_margin")


async def test_live_protective_orders_are_sent_concurrently(db, monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    in_flight, peak, types = 0, 0, []

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        q = dict(request.url.params)
        types.append(q["type"])
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        if q["type"] == "MARKET":
            return httpx.Response(200, json={"orderId": 1, "status": "FILLED", "executedQty": q["quantity"]})
        if q["type"] == "TAKE_PROFIT_MARKET":
            return httpx.Response(400, json={"code": -2021, "msg": "Order would immediately trigger."})
        return httpx.Response(200, json={"orderId": 2, "status": "NEW"})

    client = BinanceFuturesClient("k", "s", testnet=True, transport=httpx.MockTransport(handler))
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    executor = LiveExecutor(client)
    with pytest.raises(Exception, match="-2021"):
        await executor.execute(
            db, acc, None, "BTCUSDT", OrderSide.BUY, D("0.5"), D("100"), stop_loss=D("95"), reduce_only=False,
            leverage=2, tag="t", take_profit=D("110"),
        )  # fmt: skip
    await client.close()
    assert types[0] == "MARKET" and sorted(types[1:]) == ["STOP_MARKET", "TAKE_PROFIT_MARKET"]
    assert peak == 2  # SL and TP in flight together, after the entry
    stop = await db.scalar(select(Order).where(Order.type == "STOP_MARKET"))
    assert stop.exchange_order_id == "2" and stop.status == OrderStatus.NEW  # SL still recorded when TP fails
