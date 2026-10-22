"""PnL by period (/history/pnl) and CSV exports for accounting."""

import csv
import io
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D

from app.models import Trade, User, Workspace
from app.models.enums import OrderSide
from app.services.pnl_report import bucket_start, safe_text
from tests.factories import make_account, make_bot
from tests.test_api_e2e import bearer, register_login

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def test_bucket_boundaries_and_csv_injection_guard() -> None:
    ts = datetime(2026, 10, 22, 23, 59, tzinfo=timezone.utc)  # Thursday
    assert bucket_start(ts, "day") == date(2026, 10, 22)
    assert bucket_start(ts, "week") == date(2026, 10, 19)  # Monday
    assert bucket_start(ts, "month") == date(2026, 10, 1)
    assert safe_text("=HYPERLINK(1)") == "'=HYPERLINK(1)" and safe_text("conta 1") == "conta 1"


async def _seed(client, db):
    login = await register_login(client, "pnl@example.com")
    user = await db.get(User, uuid.UUID(login["user"]["id"]))
    ws = await db.get(Workspace, user.active_workspace_id)
    a1 = await make_account(db, ws, label="Conta A")
    a2 = await make_account(db, ws, label="=cmd")
    bot = await make_bot(db, a1, "ema_cross", {}, name="Bot EMA")

    def trade(acc, pnl, fee, when, bot_id=None, asset="USDT"):
        db.add(Trade(workspace_id=acc.workspace_id, account_id=acc.id, bot_id=bot_id, symbol="BTCUSDT",
                     side=OrderSide.SELL, price=D("100"), quantity=D("2"), fee=D(fee), fee_asset=asset,
                     realized_pnl=D(pnl),
                     executed_at=when))  # fmt: skip

    trade(a1, "10", "1", NOW - timedelta(days=1), bot.id)
    trade(a1, "-4", "0.5", NOW - timedelta(days=1, minutes=5))
    trade(a2, "7", "0.01", NOW - timedelta(days=2), asset="BNB")
    trade(a2, "100", "1", NOW - timedelta(days=400))  # outside the default ranges
    # another workspace's trade must never leak
    other_user, other_ws = await _other_ws(db)
    other_acc = await make_account(db, other_ws)
    trade(other_acc, "999", "0", NOW - timedelta(days=1))
    await db.commit()
    return bearer(login), a1, a2, bot


async def _other_ws(db):
    from tests.factories import make_user_workspace

    return await make_user_workspace(db)


async def test_pnl_by_period_and_groups(client, db) -> None:
    h, a1, a2, bot = await _seed(client, db)
    r = await client.get("/api/v1/history/pnl", headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["period"] == "day" and D(data["totals"]["realized_pnl"]) == D("13")
    assert D(data["totals"]["fees"]) == D("1.5") and D(data["totals"]["net_pnl"]) == D("11.5")
    assert data["totals"]["trades"] == 3 and D(data["totals"]["fees_other"]["BNB"]) == D("0.01")

    by_acc = (await client.get("/api/v1/history/pnl", params={"group_by": "account", "period": "month"},
                               headers=h)).json()  # fmt: skip
    labels = {g["label"]: D(g["realized_pnl"]) for g in by_acc["groups"]}
    assert labels == {"Conta A": D("6"), "=cmd": D("7")}
    by_strategy = (await client.get("/api/v1/history/pnl", params={"group_by": "strategy"}, headers=h)).json()
    assert {g["group"]: D(g["realized_pnl"]) for g in by_strategy["groups"]} == {"ema_cross": D("10"), "manual": D("3")}
    by_bot = (await client.get("/api/v1/history/pnl", params={"group_by": "bot", "account_id": str(a1.id)},
                               headers=h)).json()  # fmt: skip
    assert {g["label"] for g in by_bot["groups"]} == {"Bot EMA", "Manual / sem bot"}
    week = (await client.get("/api/v1/history/pnl", params={"period": "week"}, headers=h)).json()
    assert all(date.fromisoformat(b["period_start"]).weekday() == 0 for b in week["buckets"])
    # explicit long range includes the old trade; invalid ranges are rejected
    start = (NOW - timedelta(days=500)).isoformat()
    full = (await client.get("/api/v1/history/pnl", params={"period": "month", "start": start}, headers=h)).json()
    assert D(full["totals"]["realized_pnl"]) == D("113")
    params = {"start": NOW.isoformat(), "end": (NOW - timedelta(days=1)).isoformat()}
    bad = await client.get("/api/v1/history/pnl", params=params, headers=h)
    assert bad.status_code == 422


async def test_csv_exports(client, db) -> None:
    h, a1, a2, bot = await _seed(client, db)
    r = await client.get("/api/v1/history/export/trades.csv", headers=h)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    assert len(rows) == 3  # 12-month default range, workspace only
    assert {row["conta"] for row in rows} == {"Conta A", "'=cmd"}  # formula injection neutralized
    ema = next(row for row in rows if row["bot"] == "Bot EMA")
    assert ema["estrategia"] == "ema_cross" and D(ema["pnl_realizado_usdt"]) == D("10")
    assert D(ema["notional_usdt"]) == D("200") and ema["modo"] == "paper" and ema["data_hora_utc"].endswith("Z")

    r = await client.get("/api/v1/history/export/income.csv", params={"delimiter": "semicolon", "period": "month"},
                         headers=h)  # fmt: skip
    assert r.status_code == 200
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig")), delimiter=";"))
    by_acc = {}
    for row in rows:
        by_acc[row["conta"]] = by_acc.get(row["conta"], D("0")) + D(row["pnl_realizado_usdt"])
    assert by_acc == {"Conta A": D("6"), "'=cmd": D("7")}
    assert any("BNB:" in row["taxas_outros_ativos"] for row in rows)
    audit = (await client.get("/api/v1/history/audit", params={"event": "history.export"}, headers=h)).json()
    assert audit["total"] == 2
