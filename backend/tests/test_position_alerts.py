"""G-06: open-position alerts (PnL thresholds, near stop, near liquidation) for live and paper, throttled."""

from decimal import Decimal as D

import pytest

from app.core.config import settings
from app.models import ExecutionMode, OrderSide, Position
from app.services import paper_orders
from app.services.alerts import throttle
from app.services.alerts.events import EVENTS, AlertEvent
from app.services.alerts.formatting import render
from app.services.alerts.service import PENDING_KEY
from app.services.execution import PaperExecutor
from app.services.position_alerts import check_positions, evaluate_position, liquidation_prices
from app.services.reconciler import Reconciler
from tests.factories import make_account, make_user_workspace
from tests.test_protection_guard import FakeExchange, position, stop_order


@pytest.fixture(autouse=True)
def _alert_cfg(monkeypatch):
    throttle._memory.clear()
    monkeypatch.setattr(settings, "POSITION_ALERTS_ENABLED", True)
    monkeypatch.setattr(settings, "POSITION_ALERT_PNL_ROE_PCT", [-50.0, -25.0, 25.0, 50.0])
    monkeypatch.setattr(settings, "POSITION_ALERT_PNL_USDT", [-100.0, 100.0])
    monkeypatch.setattr(settings, "POSITION_ALERT_STOP_PROXIMITY_PCT", 1.0)
    monkeypatch.setattr(settings, "POSITION_ALERT_LIQ_PROXIMITY_PCT", 5.0)
    monkeypatch.setattr(settings, "POSITION_ALERT_THROTTLE_MINUTES", 30)
    yield
    throttle._memory.clear()


def pos(qty="1", entry="100", mark="100", lev=10, stop=None, paper=True) -> Position:
    return Position(symbol="BTCUSDT", quantity=D(qty), entry_price=D(entry), mark_price=D(mark), leverage=lev,
                    stop_loss=D(stop) if stop else None, is_paper=paper)  # fmt: skip


def kinds(alerts) -> dict[str, str]:
    return {a.kind: a.level for a in alerts}


def test_pnl_thresholds_pick_most_extreme_level() -> None:
    assert kinds(evaluate_position(pos(mark="102"))) == {}  # ROE 20% < 25%
    assert kinds(evaluate_position(pos(mark="103"))) == {"pnl_roe": "25"}  # ROE 30%
    assert kinds(evaluate_position(pos(mark="106", qty="20"))) == {"pnl_roe": "50", "pnl_usdt": "100"}  # 60%/120 USDT
    short = evaluate_position(pos(qty="-1", mark="103"))  # short loses 30% ROE
    assert kinds(short) == {"pnl_roe": "-25"}
    assert short[0].event == AlertEvent.POSITION_PNL and short[0].severity == "warning"
    assert evaluate_position(pos(mark="103"))[0].severity == "info"


def test_near_stop_long_and_short() -> None:
    a = evaluate_position(pos(mark="100", stop="99.5", lev=1))
    assert kinds(a) == {"stop": ""} and a[0].event == AlertEvent.POSITION_NEAR_STOP
    assert a[0].data["distance_pct"] == D("0.50")
    assert kinds(evaluate_position(pos(mark="100", stop="98", lev=1))) == {}  # 2% away
    assert kinds(evaluate_position(pos(qty="-1", mark="100", stop="100.8", lev=1))) == {"stop": ""}
    assert kinds(evaluate_position(pos(mark="99", stop="99.5", lev=1))) == {}  # crossed: stop guard's job


def test_near_liquidation_paper_estimate_and_live_exchange_price() -> None:
    # paper 20x long from 100: estimated liquidation ~95.9 -> mark 99 is ~3.1% away
    a = evaluate_position(pos(mark="99", lev=20))
    liq = [x for x in a if x.kind == "liquidation"]
    assert liq and liq[0].severity == "critical" and liq[0].data["estimated"] is True
    assert not [x for x in evaluate_position(pos(mark="100", lev=5)) if x.kind == "liquidation"]  # far away
    # live uses the exchange's liquidationPrice; none reported -> no estimate
    live = pos(mark="100", lev=20, paper=False)
    assert not [x for x in evaluate_position(live) if x.kind == "liquidation"]
    hit = [x for x in evaluate_position(live, D("97")) if x.kind == "liquidation"]
    assert hit and hit[0].data["estimated"] is False and hit[0].data["distance_pct"] == D("3.00")
    assert liquidation_prices([{"symbol": "X", "liquidationPrice": "0"}, {"symbol": "Y", "liquidationPrice": "5"}]) == {
        "X": None, "Y": D("5"),
    }  # fmt: skip


def test_events_registered_and_rendered() -> None:
    for ev in (AlertEvent.POSITION_PNL, AlertEvent.POSITION_NEAR_STOP, AlertEvent.POSITION_NEAR_LIQUIDATION):
        assert ev.value in EVENTS
    alert = evaluate_position(pos(mark="99", lev=20))[-1]
    title, msg = render(alert.event.value, alert.severity, {k: str(v) for k, v in alert.data.items()})
    assert "liquidação" in title.lower() and "BTCUSDT" in msg and "Liquidação" in msg


async def test_check_positions_throttles_and_queues(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    p = pos(mark="99", lev=20, stop="98.5")
    sent = await check_positions(db, acc, [p])
    assert {a.kind for a in sent} == {"stop", "liquidation"}
    queued = db.sync_session.info[PENDING_KEY]
    assert {q["event"] for q in queued} == {"position_near_stop", "position_near_liquidation"}
    assert all(q["account_id"] == str(acc.id) and q["dedup"] is None for q in queued)
    assert await check_positions(db, acc, [p]) == []  # throttled
    p.mark_price = D("103")  # ROE level now hit: a different key, sent once
    assert kinds(await check_positions(db, acc, [p])) == {"pnl_roe": "50"}
    db.sync_session.info.pop(PENDING_KEY, None)


async def test_disabled(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "POSITION_ALERTS_ENABLED", False)
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    assert await check_positions(db, acc, [pos(mark="99", lev=20)]) == []


async def test_paper_engine_raises_alerts(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    await db.commit()
    prices = {"BTCUSDT": D("50000")}

    async def price(symbol, testnet):
        return prices.get(symbol)

    monkeypatch.setattr(paper_orders, "price_source", price)
    res = await PaperExecutor().execute(
        db, acc, None, "BTCUSDT", OrderSide.BUY, D("0.01"), D("50000"), stop_loss=D("49000"), reduce_only=False,
        leverage=10, tag="t",
    )  # fmt: skip
    assert res.ok, res.reason
    await db.commit()
    from app.services.alerts import service as alert_service

    dispatched: list[dict] = []
    monkeypatch.setattr(alert_service, "dispatch", dispatched.append)  # alerts go out after the commit
    prices["BTCUSDT"] = D("49200")  # 0.41% above the stop, ROE -16%
    await paper_orders.process_paper_orders(db)
    assert "position_near_stop" in [p["event"] for p in dispatched]


async def test_reconciler_raises_live_alerts_with_exchange_liquidation(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    await db.commit()
    ex = FakeExchange([position(mark="100", liq="97", leverage="20")], [stop_order()])
    await Reconciler(db, acc, ex).run()
    events = {q["event"]: q for q in db.sync_session.info.get(PENDING_KEY, [])}
    assert "position_near_liquidation" in events
    assert events["position_near_liquidation"]["data"]["liquidation_price"] == 97.0
    db.sync_session.info.pop(PENDING_KEY, None)
