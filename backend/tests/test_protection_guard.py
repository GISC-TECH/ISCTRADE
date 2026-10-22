"""G-01: every live position keeps a protective stop at the exchange (reconciler guard) + restart recovery."""

from datetime import timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.exchanges.base import ExchangeAPIError
from app.models import AccountStatus, AuditLog, BotState, ExecutionMode, Order, OrderSide, OrderType, Position
from app.models.base import utcnow
from app.services.alerts.events import AlertEvent
from app.services.alerts.service import alert_from_audit
from app.services.execution import LiveExecutor, OrderService
from app.services.protection import ProtectionGuard, stop_coverage
from app.services.reconciler import Reconciler, needs_protection_check
from app.services.symbol_filters import SymbolFilters
from app.strategy.base import OrderIntent
from tests.factories import make_account, make_bot, make_user_workspace
from tests.test_risk_limits import risk


class FakeExchange:
    """Duck-typed adapter: records every order; never touches a real exchange."""

    exchange_id = "fake"

    def __init__(self, positions=None, open_orders=None, fail_stop=False, fail_open_orders=False, atr_range=2.0):
        self.positions = positions or []
        self.open_orders = open_orders or []
        self.fail_stop, self.fail_open_orders, self.atr_range = fail_stop, fail_open_orders, atr_range
        self.placed: list[dict] = []
        self.klines_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def close(self):
        return None

    async def get_positions(self, symbol=None, include_flat=False):
        return [p for p in self.positions if include_flat or D(p["positionAmt"]) != 0]

    async def get_open_orders(self, symbol=None):
        if self.fail_open_orders:
            raise ExchangeAPIError(503, None, "busy")
        return list(self.open_orders)

    async def get_order(self, symbol, order_id=None, client_order_id=None):
        return {"orderId": 1, "status": "NEW", "executedQty": "0"}

    async def get_user_trades(self, symbol, start_time=None, from_id=None, limit=500):
        return []

    async def get_symbol_filters(self, symbol):
        return SymbolFilters(symbol=symbol, tick_size=D("0.1"))

    async def get_klines(self, symbol, interval="1h", limit=500, start_time=None, end_time=None):
        self.klines_calls += 1
        r = self.atr_range / 2
        return [[i, 100, 100 + r, 100 - r, 100, 1] for i in range(limit)]

    async def place_order(self, symbol, side, order_type, quantity=None, stop_price=None, close_position=None,
                          reduce_only=None, client_order_id=None, **extra):  # fmt: skip
        if order_type == "STOP_MARKET" and self.fail_stop:
            raise ExchangeAPIError(400, -2021, "Order would immediately trigger.")
        self.placed.append({"symbol": symbol, "side": side, "type": order_type, "stop": stop_price,
                            "close": close_position, "reduce_only": reduce_only, "qty": quantity})  # fmt: skip
        return {"orderId": len(self.placed), "status": "NEW" if order_type == "STOP_MARKET" else "FILLED",
                "executedQty": "0" if order_type == "STOP_MARKET" else str(quantity), "avgPrice": "0"}  # fmt: skip


def position(symbol="BTCUSDT", qty="0.5", mark="100", liq="0", leverage="5") -> dict:
    return {"symbol": symbol, "positionAmt": qty, "entryPrice": mark, "markPrice": mark, "unRealizedProfit": "0",
            "leverage": leverage, "liquidationPrice": liq, "positionSide": "BOTH"}  # fmt: skip


def stop_order(symbol="BTCUSDT", side="SELL", trigger="95", close=True, qty="0", reduce=False, typ="STOP_MARKET"):
    return {"symbol": symbol, "side": side, "type": typ, "stopPrice": trigger, "closePosition": close,
            "reduceOnly": reduce, "origQty": qty, "executedQty": "0", "status": "NEW", "isAlgo": True}  # fmt: skip


@pytest.fixture
def live_on(monkeypatch):
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)


async def _live_account(db, **kw):
    _, ws = await make_user_workspace(db)
    return await make_account(db, ws, execution_mode=ExecutionMode.LIVE, **kw)


async def _events(db, name):
    return list(await db.scalars(select(AuditLog).where(AuditLog.event == name)))


# ----------------------------------------------------------------------------- coverage detection
def test_stop_coverage_rules() -> None:
    qty = D("2")
    assert stop_coverage([stop_order()], "BTCUSDT", qty).full
    assert stop_coverage([stop_order()], "BTCUSDT", qty).trigger == D("95")
    assert not stop_coverage([stop_order(side="BUY")], "BTCUSDT", qty).full  # wrong side
    assert not stop_coverage([stop_order(symbol="ETHUSDT")], "BTCUSDT", qty).full
    assert not stop_coverage([stop_order(typ="TAKE_PROFIT_MARKET")], "BTCUSDT", qty).full
    assert not stop_coverage([stop_order(close=False, reduce=False, qty="2")], "BTCUSDT", qty).full  # not reduce
    partial = stop_order(close=False, reduce=True, qty="1")
    assert not stop_coverage([partial], "BTCUSDT", qty).full
    assert stop_coverage([partial, stop_order(close=False, reduce=True, qty="1", trigger="96")], "BTCUSDT", qty).full
    assert stop_coverage([stop_order(close=False, reduce=True, qty="0")], "BTCUSDT", qty).full  # Bybit closeOnTrigger
    assert stop_coverage([stop_order(typ="TRAILING_STOP_MARKET", trigger="0")], "BTCUSDT", qty).full
    short = stop_coverage([stop_order(side="BUY", trigger="110"), stop_order(side="BUY", trigger="105")], "BTCUSDT",
                          D("-1"))  # fmt: skip
    assert short.full and short.trigger == D("105")  # closest to the price
    assert not stop_coverage([{**stop_order(), "status": "CANCELED"}], "BTCUSDT", qty).full


# ----------------------------------------------------------------------------- guard
async def test_protected_position_is_left_alone(db, live_on) -> None:
    acc = await _live_account(db)
    ex = FakeExchange([position()], [stop_order(trigger="94")])
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.protected == 1 and ex.placed == []
    pos = await db.scalar(select(Position).where(Position.account_id == acc.id))
    assert pos.stop_loss == D("94")


async def test_missing_stop_recreated_from_stored_stop(db, live_on) -> None:
    acc = await _live_account(db)
    db.add(Position(workspace_id=acc.workspace_id, account_id=acc.id, symbol="BTCUSDT", quantity=D("0.5"),
                    entry_price=D("100"), stop_loss=D("93.37"), is_paper=False))  # fmt: skip
    await db.flush()
    ex = FakeExchange([position()], [])
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.restored == 1
    assert ex.placed == [{"symbol": "BTCUSDT", "side": "SELL", "type": "STOP_MARKET", "stop": D("93.4"),
                          "close": True, "reduce_only": None, "qty": None}]  # fmt: skip
    stop_row = await db.scalar(select(Order).where(Order.type == OrderType.STOP_MARKET))
    assert stop_row.reason == "guard_stored:stop_loss" and stop_row.stop_price == D("93.4") and not stop_row.is_paper
    (ev,) = await _events(db, "position.stop_restored")
    assert ev.level == "warning" and ev.data["source"] == "stored"


async def test_partial_reduce_only_stop_gets_full_protection(db, live_on) -> None:
    acc = await _live_account(db)
    db.add(Position(workspace_id=acc.workspace_id, account_id=acc.id, symbol="BTCUSDT", quantity=D("0.5"),
                    entry_price=D("100"), stop_loss=D("95"), is_paper=False))  # fmt: skip
    ex = FakeExchange([position()], [stop_order(close=False, reduce=True, qty="0.2")])
    assert (await ProtectionGuard(db, acc, ex).run()).restored == 1
    assert ex.placed[0]["close"] is True


async def test_stop_from_order_history_after_restart(db, live_on) -> None:
    """Restart: the local position row lost its stop, the entry order still has it."""
    acc = await _live_account(db)
    db.add(Order(workspace_id=acc.workspace_id, account_id=acc.id, symbol="ETHUSDT", side=OrderSide.SELL,
                 type=OrderType.MARKET, quantity=D("1"), stop_loss=D("108"), client_order_id="btp-entry",
                 is_paper=False, created_at=utcnow() - timedelta(hours=2)))  # fmt: skip
    await db.flush()
    ex = FakeExchange([position("ETHUSDT", "-1", "100")], [])
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.restored == 1 and ex.placed[0]["side"] == "BUY" and ex.placed[0]["stop"] == D("108")
    assert (await _events(db, "position.stop_restored"))[0].data["source"] == "order_history"


async def test_external_position_adopted_with_atr_emergency_stop(db, live_on) -> None:
    acc = await _live_account(db)
    ex = FakeExchange([position(qty="1", mark="100")], [], atr_range=2.0)  # ATR 2 -> stop 2 x 2 = 4 below
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.restored == 1 and ex.placed[0]["stop"] == D("96") and ex.klines_calls == 1
    (ev,) = await _events(db, "position.stop_restored")
    assert ev.data["source"] == "emergency" and ev.data["external"] is True


async def test_emergency_stop_never_beyond_liquidation(db, live_on) -> None:
    acc = await _live_account(db)
    ex = FakeExchange([position(qty="1", mark="100", liq="97")], [], atr_range=10.0)  # ATR stop would be 80
    await ProtectionGuard(db, acc, ex).run()
    assert ex.placed[0]["stop"] == D("97.6")  # 80% of the 3.0 distance to liquidation


async def test_emergency_percent_mode(db, live_on, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROTECTION_EMERGENCY_STOP_MODE", "percent")
    acc = await _live_account(db)
    ex = FakeExchange([position(qty="-2", mark="200")], [])
    await ProtectionGuard(db, acc, ex).run()
    assert ex.placed[0]["stop"] == D("206") and ex.placed[0]["side"] == "BUY" and ex.klines_calls == 0


async def test_external_position_alert_only_policy(db, live_on, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROTECTION_ADOPT_EXTERNAL", "alert")
    acc = await _live_account(db)
    ex = FakeExchange([position()], [])
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.unprotected == 1 and ex.placed == []
    (ev,) = await _events(db, "position.unprotected")
    assert ev.level == "critical" and ev.data["external"] is True and "fora da plataforma" in ev.data["reason"]


async def test_failed_stop_placement_raises_unprotected_alert(db, live_on) -> None:
    acc = await _live_account(db)
    db.add(Position(workspace_id=acc.workspace_id, account_id=acc.id, symbol="BTCUSDT", quantity=D("0.5"),
                    entry_price=D("100"), stop_loss=D("95"), is_paper=False))  # fmt: skip
    ex = FakeExchange([position()], [], fail_stop=True)
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.unprotected == 1 and summary.restored == 0
    (ev,) = await _events(db, "position.unprotected")
    assert ev.level == "critical" and "-2021" in ev.data["reason"]
    payload = alert_from_audit("position.unprotected", workspace_id=acc.workspace_id, account_id=acc.id,
                               data=ev.data)  # fmt: skip
    assert payload["event"] == AlertEvent.POSITION_UNPROTECTED.value and payload["severity"] == "critical"
    assert payload["dedup"] == f"unprot:{acc.id}:BTCUSDT"


async def test_crossed_stop_closes_position(db, live_on) -> None:
    acc = await _live_account(db)
    db.add(Position(workspace_id=acc.workspace_id, account_id=acc.id, symbol="BTCUSDT", quantity=D("0.5"),
                    entry_price=D("100"), stop_loss=D("97"), is_paper=False))  # fmt: skip
    ex = FakeExchange([position(mark="96")], [])
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.closed == 1
    assert ex.placed == [{"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "stop": None, "close": None,
                          "reduce_only": True, "qty": D("0.5")}]  # fmt: skip


async def test_crossed_stop_emergency_when_closing_disabled(db, live_on, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROTECTION_CLOSE_IF_STOP_CROSSED", False)
    monkeypatch.setattr(settings, "PROTECTION_EMERGENCY_STOP_MODE", "percent")
    acc = await _live_account(db)
    db.add(Position(workspace_id=acc.workspace_id, account_id=acc.id, symbol="BTCUSDT", quantity=D("0.5"),
                    entry_price=D("100"), stop_loss=D("97"), is_paper=False))  # fmt: skip
    ex = FakeExchange([position(mark="96")], [])
    await ProtectionGuard(db, acc, ex).run()
    assert ex.placed[0]["type"] == "STOP_MARKET" and ex.placed[0]["stop"] == D("93.1")


async def test_live_trading_disabled_only_alerts(db) -> None:
    acc = await _live_account(db)
    ex = FakeExchange([position()], [])
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.unprotected == 1 and ex.placed == []
    assert "LIVE_TRADING_ENABLED=false" in (await _events(db, "position.unprotected"))[0].data["reason"]


async def test_recent_local_order_gets_grace_period(db, live_on) -> None:
    acc = await _live_account(db)
    db.add(Order(workspace_id=acc.workspace_id, account_id=acc.id, symbol="BTCUSDT", side=OrderSide.BUY,
                 type=OrderType.MARKET, quantity=D("0.5"), stop_loss=D("95"), client_order_id="btp-new",
                 is_paper=False))  # fmt: skip
    await db.flush()
    ex = FakeExchange([position()], [])
    summary = await ProtectionGuard(db, acc, ex).run()
    assert summary.skipped == 1 and ex.placed == [] and summary.unprotected == 0


async def test_unreadable_open_orders_alert(db, live_on) -> None:
    acc = await _live_account(db)
    ex = FakeExchange([position()], fail_open_orders=True)
    assert (await ProtectionGuard(db, acc, ex).run()).unprotected == 1
    assert (await _events(db, "position.unprotected"))[0].level == "warning"


async def test_guard_can_be_disabled(db, live_on, monkeypatch) -> None:
    monkeypatch.setattr(settings, "PROTECTION_GUARD_ENABLED", False)
    acc = await _live_account(db)
    ex = FakeExchange([position()], [])
    assert (await ProtectionGuard(db, acc, ex).run()).as_dict()["stops_restored"] == 0 and ex.placed == []


async def test_reconciler_run_includes_guard(db, live_on) -> None:
    acc = await _live_account(db)
    ex = FakeExchange([position()], [])
    summary = await Reconciler(db, acc, ex).run()
    assert summary["stops_restored"] == 1 and summary["positions_changed"] == 1
    assert (await _events(db, "reconcile.changes"))[0].data["stops_restored"] == 1


def test_stream_events_that_trigger_a_check() -> None:
    assert needs_protection_check({"e": "ACCOUNT_UPDATE", "a": {"P": [{"s": "BTCUSDT", "pa": "0.1"}]}})
    assert not needs_protection_check({"e": "ACCOUNT_UPDATE", "a": {"P": [{"s": "BTCUSDT", "pa": "0"}]}})
    assert needs_protection_check({"e": "ORDER_TRADE_UPDATE", "o": {"o": "STOP_MARKET", "X": "CANCELED"}})
    assert needs_protection_check({"e": "ORDER_TRADE_UPDATE", "o": {"o": "STOP_MARKET", "X": "EXPIRED"}})
    assert not needs_protection_check({"e": "ORDER_TRADE_UPDATE", "o": {"o": "MARKET", "X": "CANCELED"}})
    assert not needs_protection_check({"e": "ORDER_TRADE_UPDATE", "o": {"o": "STOP_MARKET", "X": "NEW"}})
    assert needs_protection_check({"e": "ALGO_UPDATE"})


# ----------------------------------------------------------------------------- execution remembers the stop
async def test_live_entry_remembers_its_stop_on_the_position(db, live_on) -> None:
    acc = await _live_account(db)
    ex = FakeExchange()
    res = await OrderService(LiveExecutor(ex)).submit(
        db, acc, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("99")), symbol="BTCUSDT", ref_price=D("100"),
        leverage=1, risk_state=risk(),
    )  # fmt: skip
    assert res.ok, res.reason
    pos = await db.scalar(select(Position).where(Position.account_id == acc.id))
    assert pos.stop_loss == D("99") and not pos.is_paper


# ----------------------------------------------------------------------------- restart recovery
async def test_restart_recovery_reconciles_every_live_account(db, live_on, monkeypatch) -> None:
    from app.core.database import AsyncSessionLocal
    from app.services import accounts as accounts_service
    from app.services import jobs

    acc = await _live_account(db, status=AccountStatus.STOPPED)  # e.g. stopped by the global kill switch
    db.add(Position(workspace_id=acc.workspace_id, account_id=acc.id, symbol="BTCUSDT", quantity=D("0.5"),
                    entry_price=D("100"), stop_loss=D("95"), is_paper=False))  # fmt: skip
    await db.commit()
    assert acc.id in await jobs.live_account_ids(AsyncSessionLocal)

    ex = FakeExchange([position()], [])
    monkeypatch.setattr(accounts_service, "client_for_account", lambda account: ex)
    result = await jobs.reconcile_account(AsyncSessionLocal, acc.id)
    assert result["stops_restored"] == 1 and ex.placed[0]["stop"] == D("95")


async def test_worker_start_dispatches_reconciliation(monkeypatch) -> None:
    from app.services import dispatch
    from app.tasks import signals

    sent = []
    monkeypatch.setattr(dispatch, "enqueue", lambda name, *a, countdown=None: sent.append((name, countdown)) or True)
    assert signals.recover_on_start() is False  # live trading disabled: nothing to protect
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    assert signals.recover_on_start() and sent == [("accounts.reconcile_all", 5)]


async def test_starting_live_bot_is_prepared_on_tick(db, live_on, monkeypatch) -> None:
    from app.core.database import AsyncSessionLocal
    from app.services import jobs

    acc = await _live_account(db)
    bot = await make_bot(db, acc, "ema_cross", {}, state=BotState.STARTING)
    await db.commit()
    prepared = []

    async def fake_prepare(db_, bot_, account_):
        prepared.append(bot_.id)
        raise RuntimeError("exchange down")

    monkeypatch.setattr(jobs, "prepare_live_bot", fake_prepare)
    result = await jobs.tick_bot(AsyncSessionLocal, bot.id)
    assert prepared == [bot.id] and result["status"] == "error"
    await db.refresh(bot)
    assert bot.state == BotState.ERROR and "exchange down" in bot.last_error
