from decimal import Decimal as D

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.core.security import create_access_token
from app.models import AccountStatus, AuditLog, BotState, EquitySnapshot, ExecutionMode, Order
from app.services import jobs
from app.services.market_data import StaticMarketData
from app.tasks.celery_app import celery_app
from tests.factories import candles_from_closes, make_account, make_bot, make_user_workspace

GRID = {"lower_price": 90, "upper_price": 110, "levels": 5, "quantity_per_level": 0.5, "stop_loss_pct": 5}


def test_beat_schedule_registered() -> None:
    import app.tasks.bot_tasks  # noqa: F401  (registers tasks)

    schedule = celery_app.conf.beat_schedule
    assert {s["task"] for s in schedule.values()} == {
        "bots.tick_all",
        "accounts.reconcile_all",
        "accounts.snapshot_all",
        "risk.daily_reset",
        "alerts.daily_summary",
        "scanner.run",
        "news.ingest",
        "calendar.refresh",
        "calendar.alerts",
        "paper.process_orders",
        "audit.seal",
        "audit.verify",
    }
    import app.tasks.alert_tasks  # noqa: F401
    import app.tasks.audit_tasks  # noqa: F401
    import app.tasks.news_tasks  # noqa: F401
    import app.tasks.scanner_tasks  # noqa: F401
    import app.tasks.trading_tasks  # noqa: F401

    for name in ("bots.tick", "bots.start", "accounts.snapshot", "accounts.reconcile", "risk.daily_reset",
                 "alerts.process", "alerts.deliver", "alerts.daily_summary", "scanner.run",
                 "news.ingest", "calendar.refresh", "calendar.alerts", "paper.process_orders",
                 "audit.seal", "audit.verify"):  # fmt: skip
        assert name in celery_app.tasks


async def test_due_bots_and_tick(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    running = await make_bot(db, acc, "grid", GRID)
    await make_bot(db, acc, "grid", GRID, state=BotState.STOPPED, symbol="ETHUSDT")
    stopped_acc = await make_account(db, ws, status=AccountStatus.STOPPED)
    await make_bot(db, stopped_acc, "grid", GRID)
    await db.commit()

    assert await jobs.due_bot_ids(AsyncSessionLocal) == [running.id]
    market = StaticMarketData({"BTCUSDT": candles_from_closes([101.0, 99.0])})
    out = await jobs.tick_bot(AsyncSessionLocal, running.id, market_factory=lambda a: market)
    assert out["status"] == "ok"
    market.candles["BTCUSDT"] = candles_from_closes([101.0, 99.0, 98.0, 99.5])
    out = await jobs.tick_bot(AsyncSessionLocal, running.id, market_factory=lambda a: market)
    assert out["status"] == "ok"


async def test_start_bot_paper_and_live_disabled(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    bot = await make_bot(db, acc, "grid", GRID, state=BotState.STARTING)
    await db.commit()
    assert (await jobs.start_bot(AsyncSessionLocal, bot.id))["status"] == "prepared"
    # STARTING -> RUNNING on first tick
    market = StaticMarketData({"BTCUSDT": candles_from_closes([100.0])})
    await jobs.tick_bot(AsyncSessionLocal, bot.id, market_factory=lambda a: market)
    await db.refresh(bot)
    assert bot.state == BotState.RUNNING

    live = await make_account(db, ws, execution_mode=ExecutionMode.LIVE)
    live_bot = await make_bot(db, live, "grid", GRID, state=BotState.RUNNING)
    await db.commit()
    out = await jobs.tick_bot(AsyncSessionLocal, live_bot.id, market_factory=lambda a: market)
    assert out["status"] == "error" and "LIVE_TRADING_ENABLED" in out["message"]
    await db.refresh(live_bot)
    assert live_bot.state == BotState.ERROR
    assert await db.scalar(select(func.count()).select_from(Order).where(Order.is_paper.is_(False))) == 0


async def test_snapshot_and_daily_reset(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    await db.commit()

    async def fake_price(symbol, testnet):
        return D(100)

    out = await jobs.snapshot_account(AsyncSessionLocal, acc.id, price_lookup=fake_price)
    assert D(out["equity"]) == D(10000)
    assert await db.scalar(select(func.count()).select_from(EquitySnapshot)) == 1
    assert await jobs.daily_reset(AsyncSessionLocal) == 1
    await db.refresh(acc)
    assert acc.day_start_equity == D(10000) and acc.peak_equity == D(10000)
    assert await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.event == "risk.daily_reset")) == 1
    assert await jobs.live_account_ids(AsyncSessionLocal) == []


async def _headers(db):
    user, ws = await make_user_workspace(db)
    await db.commit()
    return user, ws, {"Authorization": f"Bearer {create_access_token(str(user.id))}", "X-Workspace-ID": str(ws.id)}


async def test_start_stop_endpoints_control_bots(client, db) -> None:
    _, ws, h = await _headers(db)
    acc = await make_account(db, ws, status=AccountStatus.STOPPED)
    bot = await make_bot(db, acc, "grid", GRID, state=BotState.IDLE)
    await db.commit()

    r = await client.post(f"/api/v1/accounts/{acc.id}/start", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "running"
    await db.refresh(bot)
    assert bot.state == BotState.STARTING

    r = await client.post(f"/api/v1/accounts/{acc.id}/stop", headers=h)
    assert r.json()["status"] == "stopped"
    await db.refresh(bot)
    assert bot.state == BotState.STOPPED


async def test_start_refused_for_live_when_disabled(client, db) -> None:
    _, ws, h = await _headers(db)
    acc = await make_account(db, ws, status=AccountStatus.STOPPED, execution_mode=ExecutionMode.LIVE)
    await db.commit()
    r = await client.post(f"/api/v1/accounts/{acc.id}/start", headers=h)
    assert r.status_code == 409 and "desabilitado" in r.json()["detail"]


async def test_killed_account_needs_reset(client, db) -> None:
    _, ws, h = await _headers(db)
    acc = await make_account(db, ws, status=AccountStatus.KILLED)
    await db.commit()
    assert (await client.post(f"/api/v1/accounts/{acc.id}/start", headers=h)).status_code == 409
    r = await client.post(f"/api/v1/accounts/{acc.id}/reset-kill-switch", headers=h)
    assert r.status_code == 200 and r.json()["status"] == "stopped"
    assert (await client.post(f"/api/v1/accounts/{acc.id}/start", headers=h)).status_code == 200
