"""G-04: stop vs. estimated liquidation price, exchange leverage brackets and the platform leverage cap."""

from decimal import Decimal as D

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import settings
from app.models import ExecutionMode, OrderSide
from app.schemas.account import AccountCreate
from app.schemas.bot import BotCreate
from app.schemas.trading import ManualOrderCreate
from app.services import binance_client
from app.services.binance_client import BinanceFuturesClient
from app.services.execution import OrderService, PaperExecutor, bracket_params
from app.services.risk_manager import AccountRiskState, OrderRequest, RiskManager, Side, estimate_liquidation_price
from app.strategy.base import OrderIntent
from tests.factories import make_account, make_user_workspace


def state(max_leverage: int = 125, equity: str = "100000") -> AccountRiskState:
    e = D(equity)
    return AccountRiskState(
        equity=e, peak_equity=e, day_start_equity=e, max_leverage=max_leverage, max_risk_per_trade_pct=D("100")
    )


def test_liquidation_estimate_matches_isolated_formula() -> None:
    # 10x long, mmr 0.5%: LP = 100 * 0.9 / 0.995
    assert estimate_liquidation_price(Side.BUY, D("100"), 10, D("0.005")) == D("90") / D("0.995")
    # 10x short: LP = 100 * 1.1 / 1.005
    assert estimate_liquidation_price(Side.SELL, D("100"), 10, D("0.005")) == D("110") / D("1.005")
    # 1x long never liquidates above zero
    assert estimate_liquidation_price(Side.BUY, D("100"), 1, D("0.01")) == 0
    # the bracket "cum" moves the long liquidation price down
    assert estimate_liquidation_price(Side.BUY, D("100"), 10, D("0.01"), D("50"), D("10")) < (
        estimate_liquidation_price(Side.BUY, D("100"), 10, D("0.01"))
    )


@pytest.mark.parametrize("side,stop", [(Side.BUY, D("94")), (Side.SELL, D("106"))])
def test_stop_after_liquidation_is_rejected(side, stop) -> None:
    # 20x: liquidation ~4.5% away; a 6% stop would never trigger before liquidation
    order = OrderRequest("BTCUSDT", side, D("1"), D("100"), stop, leverage=20, maint_margin_rate=D("0.005"))
    decision = RiskManager(leverage_cap=50).evaluate(order, state())
    assert not decision.allowed and decision.reason.startswith("stop_beyond_liquidation")


def test_stop_too_close_to_liquidation_is_rejected_by_ratio() -> None:
    # 20x long, mmr 0.5%: LP ~95.48 (4.52 away). 4.0 away > 80% of the distance -> rejected; 3.0 away is fine.
    rm = RiskManager(leverage_cap=50, liq_distance_ratio=D("0.8"))
    near = OrderRequest("BTCUSDT", Side.BUY, D("1"), D("100"), D("96"), leverage=20, maint_margin_rate=D("0.005"))
    safe = OrderRequest("BTCUSDT", Side.BUY, D("1"), D("100"), D("97"), leverage=20, maint_margin_rate=D("0.005"))
    assert not rm.evaluate(near, state()).allowed
    assert rm.evaluate(safe, state()).allowed


def test_without_bracket_info_liquidation_check_is_skipped() -> None:
    order = OrderRequest("BTCUSDT", Side.BUY, D("1"), D("100"), D("50"), leverage=20)
    assert RiskManager(leverage_cap=50).evaluate(order, state()).allowed


def test_platform_leverage_cap_overrides_account_limit() -> None:
    order = OrderRequest("BTCUSDT", Side.BUY, D("1"), D("100"), D("99"), leverage=30)
    decision = RiskManager(leverage_cap=20).evaluate(order, state(max_leverage=125))
    assert not decision.allowed and decision.reason == "leverage_limit: 30x outside 1..20x"
    assert RiskManager().leverage_cap == settings.RISK_MAX_LEVERAGE_CAP == 20


def test_exchange_bracket_max_leverage_is_enforced() -> None:
    order = OrderRequest(
        "BTCUSDT", Side.BUY, D("1"), D("100"), D("99.5"), leverage=10, maint_margin_rate=D("0.01"),
        bracket_max_leverage=5,
    )  # fmt: skip
    decision = RiskManager().evaluate(order, state())
    assert not decision.allowed and decision.reason.startswith("leverage_bracket")


def test_bracket_params_picks_the_bracket_of_the_notional() -> None:
    brackets = [
        {"initialLeverage": 75, "notionalCap": 10000, "notionalFloor": 0, "maintMarginRatio": "0.005", "cum": "0"},
        {"initialLeverage": 50, "notionalCap": 50000, "notionalFloor": 10000, "maintMarginRatio": "0.01", "cum": "50"},
        {"initialLeverage": 20, "notionalCap": 1e9, "notionalFloor": 50000, "maintMarginRatio": "0.025", "cum": "800"},
    ]
    assert bracket_params(brackets, D("500")) == {
        "maint_margin_rate": D("0.005"), "maint_amount": D("0"), "bracket_max_leverage": 75,
    }  # fmt: skip
    assert bracket_params(brackets, D("20000"))["maint_margin_rate"] == D("0.01")
    assert bracket_params(brackets, D("2e9"))["bracket_max_leverage"] == 20  # above every cap: last bracket
    assert bracket_params([], D("1")) is None


async def test_paper_order_uses_default_maintenance_margin(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.PAPER, max_leverage=20)
    svc = OrderService(PaperExecutor())
    res = await svc.submit(
        db, acc, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("94")), symbol="BTCUSDT", ref_price=D("100"),
        leverage=20, risk_state=state(max_leverage=20),
    )  # fmt: skip
    assert not res.ok and res.reason.startswith("stop_beyond_liquidation")
    res = await svc.submit(
        db, acc, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("98")), symbol="BTCUSDT", ref_price=D("100"),
        leverage=20, risk_state=state(max_leverage=20),
    )  # fmt: skip
    assert res.ok


def test_schemas_refuse_leverage_above_cap(monkeypatch) -> None:
    base = {"account_id": "00000000-0000-0000-0000-000000000001", "name": "b", "strategy_key": "ema_cross",
            "symbol": "BTCUSDT"}  # fmt: skip
    assert BotCreate(**base, leverage=20).leverage == 20
    with pytest.raises(ValidationError):
        BotCreate(**base, leverage=21)
    with pytest.raises(ValidationError):
        AccountCreate(label="x", api_key="k" * 8, api_secret="s" * 8, max_leverage=125)
    with pytest.raises(ValidationError):
        ManualOrderCreate(account_id=base["account_id"], symbol="BTCUSDT", side="BUY", quantity=1, stop_loss=1,
                          leverage=30, confirm=True)  # fmt: skip
    monkeypatch.setattr(settings, "RISK_MAX_LEVERAGE_CAP", 50)
    assert BotCreate(**base, leverage=50).leverage == 50
    with pytest.raises(ValidationError):  # hard ceiling, whatever the configuration says
        BotCreate(**base, leverage=51)


def test_config_hard_ceiling_for_leverage_cap() -> None:
    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(RISK_MAX_LEVERAGE_CAP=75)


async def test_binance_leverage_brackets_are_cached() -> None:
    binance_client._BRACKETS_CACHE.clear()
    hits = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.path)
        assert request.url.path == "/fapi/v1/leverageBracket" and "signature" in request.url.params
        return httpx.Response(200, json={"symbol": "ETHUSDT", "brackets": [
            {"bracket": 2, "initialLeverage": 50, "notionalCap": 100000, "notionalFloor": 10000,
             "maintMarginRatio": 0.01, "cum": 50},
            {"bracket": 1, "initialLeverage": 75, "notionalCap": 10000, "notionalFloor": 0,
             "maintMarginRatio": 0.005, "cum": 0},
        ]})  # fmt: skip

    async with BinanceFuturesClient("k", "s", transport=httpx.MockTransport(handler)) as client:
        first = await client.get_leverage_brackets("ETHUSDT")
        again = await client.get_leverage_brackets("ETHUSDT")
    assert [b["bracket"] for b in first] == [1, 2] and again == first and len(hits) == 1
    binance_client._BRACKETS_CACHE.clear()


async def test_binance_transfers_only_wallet_movements() -> None:
    rows = [
        {"incomeType": "TRANSFER", "income": "500", "asset": "USDT", "time": 1000, "tranId": 1},
        {"incomeType": "REALIZED_PNL", "income": "-20", "asset": "USDT", "time": 1001, "tranId": 2},
        {"incomeType": "INTERNAL_TRANSFER", "income": "-100", "asset": "USDT", "time": 1002, "tranId": 3},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    async with BinanceFuturesClient("k", "s", transport=httpx.MockTransport(handler)) as client:
        out = await client.get_transfers(0)
    assert [(t["type"], t["amount"]) for t in out] == [("TRANSFER", "500"), ("INTERNAL_TRANSFER", "-100")]
