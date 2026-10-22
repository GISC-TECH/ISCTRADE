"""G-02: aggregate limits per workspace/instance and the hard capital cap of live accounts."""

from dataclasses import replace
from decimal import Decimal as D

import pytest

from app.core.config import settings
from app.models import ExecutionMode, OrderSide, Position
from app.services.execution import ExecutionResult, OrderService, PaperExecutor
from app.services.risk_limits import (
    AggregateLimits,
    capital_cap,
    get_workspace_limits,
    set_global_limits,
    set_workspace_limits,
)
from app.services.risk_manager import AccountRiskState, AggregateExposure, OrderRequest, RiskManager, Side
from app.strategy.base import OrderIntent
from tests.factories import make_account, make_user_workspace
from tests.test_api_e2e import bearer, register_login


def risk(equity="100000") -> AccountRiskState:
    e = D(equity)
    return AccountRiskState(equity=e, peak_equity=e, day_start_equity=e, max_leverage=10, max_risk_per_trade_pct=D("5"))


class FakeLive:
    """Live-mode executor stub: accepts everything, never touches an exchange."""

    is_paper = False

    async def execute(self, db, account, bot, symbol, side, quantity, ref_price, **kw) -> ExecutionResult:
        return ExecutionResult(True, "NEW", None, ref_price, quantity)


def order(qty="1", side=Side.BUY, reduce_only=False) -> OrderRequest:
    return OrderRequest("BTCUSDT", side, D(qty), D("100"), D("99") if side == Side.BUY else D("101"), 1, reduce_only)


# ----------------------------------------------------------------------------- RiskManager unit
def test_evaluate_aggregate_rules() -> None:
    rm = RiskManager()
    base = AggregateExposure("workspace", D("900"), 2, D("10"), D("1000"), D("100"), 2)
    assert rm.evaluate_aggregate(order(), [base]).allowed
    assert rm.evaluate_aggregate(order(), [replace(base, notional=D("1001"))]).reason.startswith("aggregate_notional")
    # a reducing order (notional not increasing) is never blocked by the notional cap
    assert rm.evaluate_aggregate(order(), [replace(base, notional=D("5000"), adds_notional=False)]).allowed
    assert rm.evaluate_aggregate(order(), [replace(base, daily_loss=D("100"))]).reason.startswith(
        "aggregate_daily_loss"
    )
    assert rm.evaluate_aggregate(order(), [replace(base, open_positions=3)]).reason.startswith("aggregate_positions")
    assert rm.evaluate_aggregate(order(), [replace(base, open_positions=3, opens_position=False)]).allowed
    unlimited = AggregateExposure("global", D("1e12"), 999, D("1e9"))
    assert rm.evaluate_aggregate(order(), [unlimited]).allowed
    assert rm.evaluate_aggregate(order(reduce_only=True), [replace(base, daily_loss=D("1e6"))]).allowed


def test_capital_cap_limits_sizing_and_margin() -> None:
    rm = RiskManager()
    capped = replace(risk("10000"), capital_cap=D("1000"))
    assert capped.sizing_equity == D("1000")
    # 5% of 1000 = 50 USDT of risk per trade, 1 USDT stop distance -> 50 units
    assert rm.max_position_size(D("100"), D("99"), capped) == D("50")
    big = OrderRequest("BTCUSDT", Side.BUY, D("20"), D("100"), D("99"), leverage=1)  # 2000 USDT of margin
    decision = rm.evaluate(big, capped)
    assert not decision.allowed and "capital cap" in decision.reason
    assert rm.evaluate(big, risk("10000")).allowed


async def test_capital_cap_of_live_accounts(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    paper = await make_account(db, ws)
    live = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    assert capital_cap(paper) is None
    assert capital_cap(live) == D("1000")  # instance default
    live.max_capital_usdt = D("250")
    assert capital_cap(live) == D("250")
    live.max_capital_usdt = D("5000")
    assert capital_cap(live) == D("1000")  # the instance cap always wins
    monkeypatch.setattr(settings, "RISK_LIVE_ACCOUNT_MAX_CAPITAL_USDT", 0.0)
    assert capital_cap(live) == D("5000")


async def test_order_service_applies_capital_cap_to_live(db) -> None:
    _, ws = await make_user_workspace(db)
    live = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    res = await OrderService(FakeLive()).submit(
        db, live, OrderIntent(OrderSide.BUY, D("15"), stop_loss=D("99")), symbol="BTCUSDT", ref_price=D("100"),
        leverage=1, risk_state=risk(),
    )  # fmt: skip
    assert not res.ok and res.reason.startswith("insufficient_margin") and "capital cap" in res.reason
    res = await OrderService(FakeLive()).submit(
        db, live, OrderIntent(OrderSide.BUY, D("5"), stop_loss=D("99")), symbol="BTCUSDT", ref_price=D("100"),
        leverage=1, risk_state=risk(),
    )  # fmt: skip
    assert res.ok, res.reason


# ----------------------------------------------------------------------------- aggregation over the DB
async def _live_with_position(db, ws, symbol: str, qty: str, price: str = "100", **kw):
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE, **kw)
    db.add(Position(workspace_id=ws.id, account_id=acc.id, symbol=symbol, quantity=D(qty), entry_price=D(price),
                    mark_price=D(price), leverage=1, is_paper=False))  # fmt: skip
    await db.flush()
    return acc


async def _submit(db, acc, qty="1", side=OrderSide.BUY, symbol="BTCUSDT", executor=None):
    stop = D("99") if side == OrderSide.BUY else D("101")
    return await OrderService(executor or FakeLive()).submit(
        db, acc, OrderIntent(side, D(qty), stop_loss=stop), symbol=symbol, ref_price=D("100"), leverage=1,
        risk_state=risk(),
    )  # fmt: skip


async def test_workspace_max_open_positions(db) -> None:
    _, ws = await make_user_workspace(db)
    a = await _live_with_position(db, ws, "ETHUSDT", "1")
    await _live_with_position(db, ws, "SOLUSDT", "-1")
    await set_workspace_limits(db, ws.id, AggregateLimits.build(0, 0, 2))
    res = await _submit(db, a)
    assert not res.ok and res.reason.startswith("aggregate_positions: workspace would hold 3 positions")
    res = await _submit(db, a, symbol="ETHUSDT")  # adding to an existing position is not a new one
    assert res.ok, res.reason


async def test_workspace_total_notional(db) -> None:
    _, ws = await make_user_workspace(db)
    a = await _live_with_position(db, ws, "ETHUSDT", "5")  # 500 USDT
    await set_workspace_limits(db, ws.id, AggregateLimits.build(800, 0, 0))
    assert (await _submit(db, a, qty="3")).ok  # 500 + 300 = 800 (not above)
    res = await _submit(db, a, qty="4")
    assert not res.ok and res.reason.startswith("aggregate_notional: workspace exposure 900.00 > 800.00")
    # selling ETH reduces the exposure: allowed even though the total is above the cap
    await set_workspace_limits(db, ws.id, AggregateLimits.build(100, 0, 0))
    assert (await _submit(db, a, qty="2", side=OrderSide.SELL, symbol="ETHUSDT")).ok


async def test_workspace_daily_loss_in_usdt(db) -> None:
    _, ws = await make_user_workspace(db)
    a = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    await make_account(db, ws, execution_mode=ExecutionMode.LIVE, day_start_equity=D("1000"), last_equity=D("780"))
    await set_workspace_limits(db, ws.id, AggregateLimits.build(0, 300, 0))
    a.day_start_equity = D("100000")
    risk_state = risk("99900")  # this account lost 100 today, the other 220 -> 320 >= 300
    res = await OrderService(FakeLive()).submit(
        db, a, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("99")), symbol="BTCUSDT", ref_price=D("100"),
        leverage=1, risk_state=risk_state,
    )  # fmt: skip
    assert not res.ok and res.reason.startswith("aggregate_daily_loss: workspace lost 320.00")


async def test_global_limits_span_workspaces(db) -> None:
    _, ws1 = await make_user_workspace(db)
    _, ws2 = await make_user_workspace(db)
    await _live_with_position(db, ws1, "ETHUSDT", "1")
    b = await _live_with_position(db, ws2, "SOLUSDT", "1")
    await set_global_limits(db, AggregateLimits.build(0, 0, 2))
    res = await _submit(db, b)
    assert not res.ok and res.reason.startswith("aggregate_positions: global")


async def test_paper_accounts_ignored_unless_configured(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    paper = await make_account(db, ws)
    await set_workspace_limits(db, ws.id, AggregateLimits.build(50, 0, 0))
    assert (await _submit(db, paper, executor=PaperExecutor())).ok
    monkeypatch.setattr(settings, "RISK_AGGREGATE_INCLUDE_PAPER", True)
    res = await _submit(db, paper, executor=PaperExecutor())
    assert not res.ok and res.reason.startswith("aggregate_notional")


async def test_live_and_paper_are_aggregated_separately(db) -> None:
    _, ws = await make_user_workspace(db)
    paper = await make_account(db, ws)
    db.add(Position(workspace_id=ws.id, account_id=paper.id, symbol="ETHUSDT", quantity=D("1000"),
                    entry_price=D("100"), mark_price=D("100"), leverage=1, is_paper=True))  # fmt: skip
    live = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    await set_workspace_limits(db, ws.id, AggregateLimits.build(500, 0, 0))
    assert (await _submit(db, live)).ok


async def test_default_workspace_limits_come_from_env(db) -> None:
    _, ws = await make_user_workspace(db)
    limits = await get_workspace_limits(db, ws.id)
    assert limits == AggregateLimits(D("10000"), D("300"), 5)


# ----------------------------------------------------------------------------- API permissions
async def _setup_members(client):
    owner_login = await register_login(client, "admin-owner@example.com")  # first user = platform super-admin
    superadmin = bearer(owner_login)
    ws_owner = bearer(await register_login(client, "ws-owner@example.com"))
    trader = bearer(await register_login(client, "ws-trader@example.com"))
    client.cookies.clear()
    ws_id = (await client.get("/api/v1/workspaces/", headers=ws_owner)).json()[0]["id"]
    r = await client.post(
        f"/api/v1/workspaces/{ws_id}/members", json={"email": "ws-trader@example.com", "role": "trader"},
        headers=ws_owner,
    )  # fmt: skip
    assert r.status_code == 201
    return superadmin, {**ws_owner, "X-Workspace-ID": ws_id}, {**trader, "X-Workspace-ID": ws_id}, ws_id


async def test_risk_limits_api_permissions(client) -> None:
    superadmin, owner, trader, ws_id = await _setup_members(client)
    r = await client.get("/api/v1/risk/limits", headers=trader)
    assert r.status_code == 200
    body = r.json()
    assert body["workspace"]["max_open_positions"] == 5 and body["max_leverage_cap"] == 20
    assert D(body["live_account_max_capital_usdt"]) == D("1000")

    # trader: tighten ok, loosen forbidden
    r = await client.put("/api/v1/risk/limits", json={"max_open_positions": 3}, headers=trader)
    assert r.status_code == 200 and r.json()["max_open_positions"] == 3
    r = await client.put("/api/v1/risk/limits", json={"max_open_positions": 4}, headers=trader)
    assert r.status_code == 403
    # workspace admin: may go back up to the server defaults, not above, nor disable a limit
    r = await client.put("/api/v1/risk/limits", json={"max_open_positions": 5}, headers=owner)
    assert r.status_code == 200
    for change in ({"max_open_positions": 6}, {"max_daily_loss_usdt": None}, {"max_total_notional_usdt": 0}):
        assert (await client.put("/api/v1/risk/limits", json=change, headers=owner)).status_code == 403, change
    # platform super-admin can (acting in the workspace)
    url = f"/api/v1/risk/workspaces/{ws_id}/limits"
    assert (await client.put(url, json={"max_open_positions": 50}, headers=owner)).status_code == 403
    r = await client.put(url, json={"max_open_positions": 50}, headers=superadmin)
    assert r.status_code == 200 and r.json()["max_open_positions"] == 50
    assert (await client.get("/api/v1/risk/limits", headers=trader)).json()["workspace"]["max_open_positions"] == 50

    # global limits: super-admin only
    assert (await client.get("/api/v1/risk/global-limits", headers=owner)).status_code == 403
    r = await client.put("/api/v1/risk/global-limits", json={"max_total_notional_usdt": "50000"}, headers=superadmin)
    assert r.status_code == 200 and D(r.json()["max_total_notional_usdt"]) == D("50000")
    r = await client.get("/api/v1/risk/limits", headers=trader)
    assert D(r.json()["global_limits"]["max_total_notional_usdt"]) == D("50000")


@pytest.mark.parametrize("value,status", [("1500", 422), ("500", 201)])
async def test_account_capital_cap_bounded_by_instance(client, value, status) -> None:
    _, owner, _, _ = await _setup_members(client)
    body = {"label": "L", "api_key": "placeholder-key-1234", "api_secret": "placeholder-secret-5678",
            "max_capital_usdt": value}  # fmt: skip
    r = await client.post("/api/v1/accounts/", json=body, headers=owner)
    assert r.status_code == status, r.text


async def test_trader_cannot_raise_or_remove_capital_cap(client) -> None:
    _, owner, trader, _ = await _setup_members(client)
    body = {"label": "L", "api_key": "placeholder-key-1234", "api_secret": "placeholder-secret-5678",
            "max_capital_usdt": "300"}  # fmt: skip
    acc = (await client.post("/api/v1/accounts/", json=body, headers=owner)).json()
    assert D(acc["max_capital_usdt"]) == D("300")
    url = f"/api/v1/accounts/{acc['id']}"
    assert (await client.patch(url, json={"max_capital_usdt": "200"}, headers=trader)).status_code == 200
    assert (await client.patch(url, json={"max_capital_usdt": "400"}, headers=trader)).status_code == 403
    assert (await client.patch(url, json={"max_capital_usdt": None}, headers=trader)).status_code == 403
    assert (await client.patch(url, json={"max_capital_usdt": "400"}, headers=owner)).status_code == 200
