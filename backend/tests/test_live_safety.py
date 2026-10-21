"""Live-trading safety nets: failed protective stop, robust kill switch (exchange always mocked)."""

from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import AuditLog, ExecutionMode, OrderSide, OrderType
from app.services import accounts as accounts_service
from app.services.execution import LiveExecutor
from app.services.kill_switch import close_live_positions, trigger_account_kill_switch
from tests.factories import make_account, make_user_workspace


class Fake:
    def __init__(self, *, fail_types=(), fill="0", positions=None, open_orders=None, fail_symbols=()):
        self.calls: list[tuple[str, dict]] = []
        self.fail_types, self.fill = set(fail_types), fill
        self.positions, self.open_orders, self.fail_symbols = positions or [], open_orders or [], set(fail_symbols)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        if kw["order_type"] in self.fail_types or kw.get("symbol") in self.fail_symbols:
            raise RuntimeError(f"{kw['order_type']} boom")
        status = "FILLED" if self.fill != "0" else "NEW"
        return {"orderId": len(self.calls), "status": status, "executedQty": self.fill, "avgPrice": "50000"}

    async def cancel_order(self, symbol, *, order_id=None, client_order_id=None):
        self.calls.append(("cancel_order", {"symbol": symbol, "client_order_id": client_order_id}))
        return {"status": "CANCELED", "executedQty": "0"}

    async def cancel_all_orders(self, symbol):
        self.calls.append(("cancel_all_orders", {"symbol": symbol}))
        if symbol in self.fail_symbols:
            raise RuntimeError("cancel boom")
        return {}

    async def get_positions(self):
        return self.positions

    async def get_open_orders(self, symbol=None):
        return self.open_orders


@pytest.fixture
def live(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)


async def _exec(db, fake, order_type=OrderType.MARKET, **kw):
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    res = await LiveExecutor(fake).execute(
        db, acc, None, "BTCUSDT", OrderSide.BUY, D("0.01"), D("50000"), stop_loss=D("49000"), reduce_only=False,
        leverage=2, tag="t", order_type=order_type, limit_price=D("49500") if order_type == OrderType.LIMIT else None,
        **kw,
    )  # fmt: skip
    return res


async def test_failed_stop_flattens_filled_market_entry(db, live) -> None:
    fake = Fake(fail_types={"STOP_MARKET"}, fill="0.01")
    res = await _exec(db, fake)
    assert not res.ok and res.reason.startswith("protective_stop_failed")
    close = fake.calls[-1][1]
    assert close["order_type"] == "MARKET" and close["side"] == "SELL" and close["reduce_only"]
    assert close["quantity"] == D("0.01")
    ev = await db.scalar(select(AuditLog).where(AuditLog.event == "order.protection_failed"))
    assert ev is not None


async def test_failed_stop_cancels_resting_limit_entry(db, live) -> None:
    fake = Fake(fail_types={"STOP_MARKET"})
    res = await _exec(db, fake, OrderType.LIMIT)
    assert not res.ok
    names = [n for n, _ in fake.calls]
    assert names == ["place_order", "place_order", "cancel_order"]  # nothing filled -> no closing order


async def test_failed_take_profit_keeps_trade(db, live) -> None:
    fake = Fake(fail_types={"TAKE_PROFIT_MARKET"}, fill="0.01")
    res = await _exec(db, fake, take_profit=D("52000"))
    assert res.ok
    assert [kw["order_type"] for _, kw in fake.calls] == ["MARKET", "STOP_MARKET", "TAKE_PROFIT_MARKET"]


async def test_kill_switch_continues_after_failure_and_cancels_all_symbols(db, live, monkeypatch) -> None:
    fake = Fake(
        positions=[{"symbol": "ETHUSDT", "positionAmt": "1"}, {"symbol": "BTCUSDT", "positionAmt": "-0.5"}],
        open_orders=[{"symbol": "SOLUSDT", "clientOrderId": "x"}],
        fail_symbols={"ETHUSDT"},
    )
    monkeypatch.setattr(accounts_service, "client_for_account", lambda account: fake)
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    assert await close_live_positions(acc) == 1  # ETH failed, BTC still closed
    canceled = {kw["symbol"] for n, kw in fake.calls if n == "cancel_all_orders"}
    assert canceled == {"ETHUSDT", "BTCUSDT", "SOLUSDT"}
    btc = [kw for n, kw in fake.calls if n == "place_order" and kw["symbol"] == "BTCUSDT"][0]
    assert btc["side"] == "BUY" and btc["quantity"] == D("0.5") and btc["reduce_only"]


async def test_kill_switch_without_close_keeps_protective_stops(db, live, monkeypatch) -> None:
    from app.core.config import settings

    fake = Fake(open_orders=[
        {"symbol": "BTCUSDT", "clientOrderId": "entry", "reduceOnly": False, "closePosition": False},
        {"symbol": "BTCUSDT", "clientOrderId": "stop", "reduceOnly": True, "closePosition": True},
    ])  # fmt: skip
    monkeypatch.setattr(accounts_service, "client_for_account", lambda account: fake)
    monkeypatch.setattr(settings, "KILL_SWITCH_CLOSE_POSITIONS", False)
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    await trigger_account_kill_switch(db, acc, "test", close_positions=False)
    assert [kw["client_order_id"] for n, kw in fake.calls if n == "cancel_order"] == ["entry"]
