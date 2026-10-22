"""Auto-sizing never exceeds max risk per trade (regression: short entries rejected with risk 100.51 > 100.00)."""

from decimal import Decimal as D

from app.models import ExecutionMode, OrderSide
from app.services.execution import OrderService, PaperExecutor
from app.services.symbol_filters import SymbolFilters
from app.strategy.base import OrderIntent
from tests.factories import make_account, make_user_workspace
from tests.test_risk_limits import risk

LOW = SymbolFilters("LOWUSDT", tick_size=D("0.0001"), step_size=D("1"), min_qty=D("1"), min_notional=D("5"))


async def _submit(db, side, ref, stop, filters=LOW, **kw):
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.PAPER, max_leverage=10)
    return await OrderService(PaperExecutor()).submit(
        db, acc, OrderIntent(side, None, stop_loss=D(stop), risk_fraction=D("1")), symbol=filters.symbol,
        ref_price=D(ref), leverage=5, risk_state=risk("2000"), filters=filters, **kw,
    )  # fmt: skip


async def test_short_full_risk_fraction_with_stop_rounded_up_is_accepted(db) -> None:
    # stop 0.52236 -> tick-rounded 0.5224: sizing on the raw stop gave 9940 x 0.0101 = 100.39 USDT > 100
    res = await _submit(db, OrderSide.SELL, "0.5123", "0.52236")
    assert res.ok, res.reason
    assert res.quantity == D("9900")
    assert res.quantity * (D("0.5224") - D("0.5123")) <= D("100")


async def test_long_full_risk_fraction_stays_within_limit(db) -> None:
    res = await _submit(db, OrderSide.BUY, "0.5123", "0.50224")  # rounds down to 0.5022 (wider stop)
    assert res.ok, res.reason
    assert res.quantity * (D("0.5123") - D("0.5022")) <= D("100")


def test_fit_risk_steps_quantity_down() -> None:
    svc = OrderService(PaperExecutor())
    state = risk("2000")  # max risk 100 USDT
    assert svc._fit_risk(D("10000"), D("1"), D("0.99"), state, LOW, True) == D("10000")  # exactly 100
    assert svc._fit_risk(D("10002"), D("1"), D("0.99"), state, LOW, True) == D("10000")
