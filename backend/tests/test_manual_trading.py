"""Manual trading (real-time panel): RiskManager, mandatory SL, LIMIT/TP, kill switches, audit and alerts.

Market data and the Binance client are always mocked.
"""

import uuid
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.models import Account, AccountStatus, AuditLog, ExecutionMode, Order, OrderStatus, Position
from app.services import manual_trading, paper_orders
from app.services.alerts import service as alert_service
from app.services.kill_switch import set_global_kill_switch
from app.services.manual_trading import ManualOrderRequest, ManualTradingError
from app.services.symbol_filters import SymbolFilters
from tests.factories import make_account, make_user_workspace

PW = "s3nha-forte-123"
PRICES: dict[str, D] = {}


@pytest.fixture(autouse=True)
def market(monkeypatch):
    PRICES.clear()
    PRICES["BTCUSDT"] = D("50000")

    async def price(symbol: str, testnet: bool):
        return PRICES.get(symbol)

    async def filters(symbol: str, testnet: bool):
        return SymbolFilters(symbol=symbol, tick_size=D("0.1"), step_size=D("0.001"), min_qty=D("0.001"))

    monkeypatch.setattr(manual_trading, "price_source", price)
    monkeypatch.setattr(manual_trading, "filters_source", filters)
    monkeypatch.setattr(paper_orders, "price_source", price)


@pytest.fixture
def alerts(monkeypatch):
    out: list[dict] = []
    monkeypatch.setattr(alert_service, "dispatch", lambda payload: out.append(payload) or True)
    return out


async def _login_with_account(client, email="trader@example.com") -> tuple[dict, str]:
    r = await client.post("/api/v1/auth/register", json={"email": email, "password": PW, "full_name": "T"})
    assert r.status_code == 201, r.text
    login = (await client.post("/api/v1/auth/login", json={"email": email, "password": PW})).json()
    h = {"Authorization": f"Bearer {login['access_token']}"}
    acc = await client.post(
        "/api/v1/accounts/",
        json={"label": "P", "api_key": "placeholder-key-0001", "api_secret": "placeholder-secret-0001"},
        headers=h,
    )
    assert acc.status_code == 201, acc.text
    return h, acc.json()["id"]


def _order(acc_id: str, **kw) -> dict:
    body = {"account_id": acc_id, "symbol": "btcusdt", "side": "BUY", "type": "MARKET", "quantity": "0.01",
            "stop_loss": "49000", "confirm": True}  # fmt: skip
    body.update(kw)
    return {k: v for k, v in body.items() if v is not None}


async def test_market_order_with_sl_tp_then_close(client, db, alerts) -> None:
    h, acc_id = await _login_with_account(client)
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id, take_profit="52000", leverage=2), headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["mode"] == "paper" and body["status"] == "FILLED"
    assert body["order"]["take_profit"] == "52000.0" or D(body["order"]["take_profit"]) == D("52000")

    pos = await db.scalar(select(Position).where(Position.symbol == "BTCUSDT"))
    assert pos.quantity == D("0.01") and pos.stop_loss == D("49000") and pos.take_profit == D("52000")
    assert pos.leverage == 2
    events = {a["event"] for a in alerts}
    assert "manual_action" in events
    manual = next(a for a in alerts if a["event"] == "manual_action")
    assert manual["data"]["action"] == "order" and manual["data"]["result"] == "ok"
    audit = await db.scalar(select(AuditLog).where(AuditLog.event == "manual.order"))
    assert audit is not None and audit.user_id is not None

    positions = (await client.get("/api/v1/history/positions", headers=h)).json()
    assert positions and D(positions[0]["take_profit"]) == D("52000")

    PRICES["BTCUSDT"] = D("51000")
    r = await client.post(
        "/api/v1/trading/close", json={"account_id": acc_id, "symbol": "BTCUSDT", "confirm": True}, headers=h
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and D(r.json()["realized_pnl"]) > 0
    await db.refresh(pos)
    assert pos.quantity == 0 and pos.take_profit is None and pos.stop_loss is None
    # closing again is a no-op
    r = await client.post(
        "/api/v1/trading/close", json={"account_id": acc_id, "symbol": "BTCUSDT", "confirm": True}, headers=h
    )
    assert r.json()["status"] == "no_position"


async def test_validation_sl_required_confirm_and_sides(client) -> None:
    h, acc_id = await _login_with_account(client)
    assert (
        await client.post("/api/v1/trading/orders", json=_order(acc_id, stop_loss=None), headers=h)
    ).status_code == 422
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id, confirm=False), headers=h)
    assert r.status_code == 400
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id, stop_loss="51000"), headers=h)
    assert r.status_code == 200 and not r.json()["ok"] and r.json()["reason"].startswith("stop_loss_invalid")
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id, take_profit="48000"), headers=h)
    assert not r.json()["ok"] and r.json()["reason"].startswith("take_profit_invalid")
    # risk limit: 1 BTC with a 1000 USDT stop distance risks 1000 > 1% of 10k
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id, quantity="1"), headers=h)
    assert not r.json()["ok"] and r.json()["reason"].startswith("max_risk_per_trade")
    # leverage above the account limit (5x) is rejected by the RiskManager
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id, leverage=10), headers=h)
    assert not r.json()["ok"] and r.json()["reason"].startswith("leverage_limit")
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id, type="LIMIT"), headers=h)
    assert r.status_code == 422  # LIMIT without price
    PRICES.pop("BTCUSDT")
    assert (await client.post("/api/v1/trading/orders", json=_order(acc_id), headers=h)).status_code == 503


async def test_limit_order_rests_fills_and_take_profit(client, db) -> None:
    h, acc_id = await _login_with_account(client)
    body = _order(acc_id, type="LIMIT", price="49500", stop_loss="48500", take_profit="51000")
    r = await client.post("/api/v1/trading/orders", json=body, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "NEW" and r.json()["reason"] == "resting"
    order_id = r.json()["order"]["id"]
    assert (await db.scalar(select(Position).where(Position.symbol == "BTCUSDT"))) is None

    report = await paper_orders.process_paper_orders(db)  # price 50000: not crossed yet
    assert report.filled == 0
    PRICES["BTCUSDT"] = D("49400")
    report = await paper_orders.process_paper_orders(db)
    assert report.filled == 1
    order = await db.get(Order, uuid.UUID(order_id))
    await db.refresh(order)
    assert order.status == OrderStatus.FILLED and order.avg_price == D("49500")
    pos = await db.scalar(select(Position).where(Position.symbol == "BTCUSDT"))
    assert pos.quantity == D("0.01") and pos.entry_price == D("49500") and pos.take_profit == D("51000")

    PRICES["BTCUSDT"] = D("51200")
    report = await paper_orders.process_paper_orders(db)
    assert report.take_profits == 1
    await db.refresh(pos)
    assert pos.quantity == 0
    assert await db.scalar(select(AuditLog).where(AuditLog.event == "position.take_profit"))


async def test_paper_stop_loss_short(db) -> None:
    user, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    await db.commit()
    req = ManualOrderRequest(symbol="BTCUSDT", side="SELL", type="MARKET", quantity=D("0.01"), stop_loss=D("51000"))
    res = await manual_trading.place_order(db, acc, _req(req), user.id)
    await db.commit()
    assert res.ok
    PRICES["BTCUSDT"] = D("51500")
    report = await paper_orders.process_paper_orders(db)
    assert report.stops == 1
    pos = await db.scalar(select(Position).where(Position.account_id == acc.id))
    assert pos.quantity == 0


def _req(req: ManualOrderRequest) -> ManualOrderRequest:
    from app.models import OrderSide, OrderType

    req.side, req.type = OrderSide(req.side), OrderType(req.type)
    return req


async def test_kill_switches_block_openings_but_allow_closing(client, db, alerts) -> None:
    h, acc_id = await _login_with_account(client)
    assert (await client.post("/api/v1/trading/orders", json=_order(acc_id), headers=h)).json()["ok"]
    resting = await client.post(
        "/api/v1/trading/orders", json=_order(acc_id, type="LIMIT", price="49800", stop_loss="49000"), headers=h
    )
    assert resting.json()["status"] == "NEW"

    acc = await db.get(Account, uuid.UUID(acc_id))
    await set_global_kill_switch(db, True, None, "test")
    await db.commit()
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id), headers=h)
    assert r.status_code == 409 and "global" in r.json()["detail"]
    lev = {"account_id": acc_id, "symbol": "BTCUSDT", "leverage": 2, "confirm": True}
    assert (await client.post("/api/v1/trading/leverage", json=lev, headers=h)).status_code == 409
    assert await db.scalar(select(AuditLog).where(AuditLog.event == "manual.order_blocked"))
    report = await paper_orders.process_paper_orders(db)
    assert report.canceled == 1  # resting entry canceled under the kill switch

    await set_global_kill_switch(db, False, None)
    await db.refresh(acc)
    acc.status = AccountStatus.KILLED
    await db.commit()
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id), headers=h)
    assert r.status_code == 409 and "conta" in r.json()["detail"]

    r = await client.post("/api/v1/trading/close-all", json={"account_id": acc_id, "confirm": True}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["closed"] == ["BTCUSDT"] and r.json()["failed"] == []
    assert any(a["event"] == "manual_action" and a["data"]["action"] == "close_all" for a in alerts)
    assert any(a["event"] == "manual_action" and a["severity"] == "warning" for a in alerts)  # blocked attempt


async def test_cancel_leverage_and_isolation(client, db) -> None:
    h, acc_id = await _login_with_account(client)
    r = await client.post(
        "/api/v1/trading/orders", json=_order(acc_id, type="LIMIT", price="49000", stop_loss="48000"), headers=h
    )
    oid = r.json()["order"]["id"]
    r = await client.delete(f"/api/v1/trading/orders/{oid}", params={"account_id": acc_id}, headers=h)
    assert r.status_code == 200 and r.json()["status"] == "CANCELED"
    r = await client.delete(f"/api/v1/trading/orders/{oid}", params={"account_id": acc_id}, headers=h)
    assert r.status_code == 409

    lev = {"account_id": acc_id, "symbol": "BTCUSDT", "leverage": 3, "confirm": True}
    r = await client.post("/api/v1/trading/leverage", json=lev, headers=h)
    assert r.status_code == 200 and r.json()["leverage"] == 3
    r = await client.post("/api/v1/trading/leverage", json={**lev, "leverage": 6}, headers=h)
    assert r.status_code == 422

    h2, _ = await _login_with_account(client, "other@example.com")
    r = await client.post("/api/v1/trading/orders", json=_order(acc_id), headers=h2)
    assert r.status_code == 404


class FakeLiveClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def close(self) -> None:
        return None

    async def get_symbol_filters(self, symbol):
        return SymbolFilters(symbol=symbol, tick_size=D("0.1"))

    async def get_account(self):
        return {"totalWalletBalance": "10000", "totalUnrealizedProfit": "0", "availableBalance": "10000"}

    async def place_order(self, **kw):
        self.calls.append(("place_order", kw))
        return {"orderId": len(self.calls), "status": "NEW", "executedQty": "0", "avgPrice": "0"}

    async def set_leverage(self, symbol, leverage):
        self.calls.append(("set_leverage", {"symbol": symbol, "leverage": leverage}))
        return {}


async def test_live_limit_order_sends_protective_orders(db, monkeypatch) -> None:
    from app.core.config import settings

    user, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    await db.commit()
    req = _req(ManualOrderRequest(symbol="BTCUSDT", side="BUY", type="LIMIT", quantity=D("0.01"),
                                  stop_loss=D("49000"), price=D("49500"), take_profit=D("52000")))  # fmt: skip
    with pytest.raises(ManualTradingError) as err:  # live accounts need the server flag
        await manual_trading.place_order(db, acc, req, user.id)
    assert err.value.status_code == 409

    fake = FakeLiveClient()
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    monkeypatch.setattr(manual_trading, "client_factory", lambda account: fake)
    res = await manual_trading.place_order(db, acc, req, user.id)
    assert res.ok
    types = [kw["order_type"] for name, kw in fake.calls if name == "place_order"]
    assert types == ["LIMIT", "STOP_MARKET", "TAKE_PROFIT_MARKET"]
    limit = fake.calls[0][1]
    assert limit["price"] == D("49500") and limit["time_in_force"] == "GTC"
    assert fake.calls[1][1]["close_position"] and fake.calls[2][1]["stop_price"] == D("52000")

    await manual_trading.set_leverage(db, acc, "BTCUSDT", 3, user.id)
    assert fake.calls[-1] == ("set_leverage", {"symbol": "BTCUSDT", "leverage": 3})
