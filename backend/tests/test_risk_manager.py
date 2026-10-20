from decimal import Decimal as D

import pytest

from app.services.risk_manager import AccountRiskState, OrderRequest, RiskManager, Side

rm = RiskManager()


def account(**kw) -> AccountRiskState:
    base = dict(equity=D("10000"), peak_equity=D("10000"), day_start_equity=D("10000"))
    base.update(kw)
    return AccountRiskState(**base)


def order(**kw) -> OrderRequest:
    # Long 1 BTC @ 50000, stop 49950 -> risk 50 USDT (0.5% of 10k)
    base = dict(
        symbol="BTCUSDT", side=Side.BUY, quantity=D("1"), entry_price=D("50000"), stop_loss=D("49950"), leverage=3
    )
    base.update(kw)
    return OrderRequest(**base)


def test_valid_order_passes() -> None:
    assert rm.validate_order(order(), account()) == (True, "ok")


def test_stop_loss_is_mandatory() -> None:
    ok, reason = rm.validate_order(order(stop_loss=None), account())
    assert not ok and reason.startswith("stop_loss_required")


@pytest.mark.parametrize(
    "side,stop",
    [(Side.BUY, D("50001")), (Side.BUY, D("50000")), (Side.SELL, D("49999"))],
)
def test_stop_loss_wrong_side(side, stop) -> None:
    ok, reason = rm.validate_order(order(side=side, stop_loss=stop), account())
    assert not ok and reason.startswith("stop_loss_invalid")


def test_short_with_valid_stop_passes() -> None:
    ok, _ = rm.validate_order(order(side=Side.SELL, stop_loss=D("50050")), account())
    assert ok


def test_max_risk_per_trade() -> None:
    # risk = 1 * 200 = 200 USDT > 1% of 10k (100)
    ok, reason = rm.validate_order(order(stop_loss=D("49800")), account())
    assert not ok and reason.startswith("max_risk_per_trade")


def test_risk_exactly_at_limit_passes() -> None:
    ok, _ = rm.validate_order(order(stop_loss=D("49900")), account())  # risk = 100 == 1%
    assert ok


@pytest.mark.parametrize("lev", [0, 6, 125])
def test_leverage_limit(lev) -> None:
    ok, reason = rm.validate_order(order(leverage=lev), account(max_leverage=5))
    assert not ok and reason.startswith("leverage_limit")


def test_max_daily_loss() -> None:
    acc = account(equity=D("9700"), peak_equity=D("10000"), day_start_equity=D("10000"))  # -3%
    ok, reason = rm.validate_order(order(), acc)
    assert not ok and reason.startswith("max_daily_loss")


def test_drawdown_kill_switch() -> None:
    acc = account(equity=D("8500"), peak_equity=D("10000"), day_start_equity=D("8500"))  # -15% from peak
    decision = rm.evaluate(order(), acc)
    assert not decision.allowed and decision.kill_switch
    assert decision.reason.startswith("kill_switch")
    assert rm.is_kill_switch_triggered(acc)


def test_reduce_only_allowed_even_with_kill_switch() -> None:
    acc = account(equity=D("5000"), peak_equity=D("10000"), day_start_equity=D("10000"))
    ok, _ = rm.validate_order(order(reduce_only=True, stop_loss=None, leverage=50), acc)
    assert ok


def test_invalid_quantity() -> None:
    ok, reason = rm.validate_order(order(quantity=D("0")), account())
    assert not ok and reason.startswith("invalid_order")


def test_max_position_size() -> None:
    qty = rm.max_position_size(D("50000"), D("49900"), account())
    assert qty == D("1")
