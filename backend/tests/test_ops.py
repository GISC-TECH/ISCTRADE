"""Operations: readiness (beat/workers/WS/ticks), Docker healthchecks, heartbeats, dead man's switch,
Alertmanager webhook, retention cleanup and new metrics."""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest
from prometheus_client import REGISTRY

from app import healthcheck
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import AlertRecord, AuditLog, EquitySnapshot, RefreshToken
from app.services import health as health_service
from app.services import heartbeats, retention
from app.services.binance_http import RateLimitGuard
from app.tasks import beat as beat_module
from app.tasks import ops_tasks
from app.workers.streams import StreamSupervisor
from tests.factories import make_account, make_user_workspace

OK = {"ok": True, "latency_ms": 0.1}


# ------------------------------------------------------------------ streams evaluation
def test_evaluate_streams_missing_and_stale() -> None:
    assert health_service.evaluate_streams(None, None)["state"] == "missing"
    stale = health_service.evaluate_streams(settings.STREAMS_HEARTBEAT_STALE_SECONDS + 1, None)
    assert stale["ok"] is False and stale["state"] == "stale"


def test_evaluate_streams_checks_each_websocket() -> None:
    now = time.time()
    status = {"streams": [
        {"name": "market-mainnet", "kind": "market", "connected": True, "last_message_at": now - 2},
        {"name": "user-1", "kind": "user", "connected": True, "last_message_at": None},  # quiet account is fine
    ]}  # fmt: skip
    ok = health_service.evaluate_streams(3.0, status, now=now)
    assert ok["ok"] is True and ok["state"] == "ok" and len(ok["streams"]) == 2
    assert ok["streams"][0]["last_message_age_seconds"] == 2.0

    status["streams"][0]["last_message_at"] = now - settings.STREAMS_MESSAGE_STALE_SECONDS - 5
    status["streams"][1]["connected"] = False
    bad = health_service.evaluate_streams(3.0, status, now=now)
    assert bad["ok"] is False and bad["state"] == "degraded"
    assert any("sem mensagens" in p for p in bad["problems"]) and any("desconectado" in p for p in bad["problems"])


def test_supervisor_status_reports_real_connection_state() -> None:
    sup = StreamSupervisor()
    market = SimpleNamespace(name="market-testnet", connected=True, last_message_at=time.time() - 1, connections=3)
    user = SimpleNamespace(name="user-x", connected=False, last_message_at=None, connections=0)
    sup.market[True] = (market, None, frozenset({"BTCUSDT"}))
    sup.user_streams["x"] = (user, None)  # type: ignore[index]
    sup.feeds = [SimpleNamespace(stream=None)]
    st = sup.status()
    kinds = {s["name"]: (s["kind"], s["connected"]) for s in st["streams"]}
    assert kinds == {"market-testnet": ("market", True), "user-x": ("user", False)}
    age = REGISTRY.get_sample_value("btp_ws_last_message_age_seconds", {"stream": "user-x", "kind": "user"})
    assert age == -1
    sup.user_streams.clear()  # account stopped: its series disappears (no eternal "disconnected" alert)
    sup.status()
    assert REGISTRY.get_sample_value("btp_ws_last_message_age_seconds", {"stream": "user-x", "kind": "user"}) is None


# ------------------------------------------------------------------ readiness
async def _patch_all_ok(monkeypatch, *, beat=5.0, workers=5.0, streams=None) -> None:
    async def ok():
        return OK

    async def ages(key):
        if key == heartbeats.BEAT_KEY:
            return beat
        if key.startswith("heartbeat:worker:"):
            return workers
        return 1.0

    async def streams_ok():
        return streams or {"ok": True, "state": "ok", "age_seconds": 1.0, "streams": []}

    for name in ("check_database", "check_redis", "check_broker"):
        monkeypatch.setattr(health_service, name, ok)
    monkeypatch.setattr(health_service, "key_age", ages)
    monkeypatch.setattr(health_service, "check_streams", streams_ok)


async def test_readiness_reports_beat_workers_and_strict_mode(db, monkeypatch) -> None:
    await _patch_all_ok(monkeypatch)
    res = await health_service.readiness()
    assert res["status"] == "ready" and health_service.http_status(res) == 200
    assert set(res["checks"]) >= {"beat", "worker_trading", "worker_default", "streams", "bot_ticks"}

    await _patch_all_ok(monkeypatch, beat=settings.BEAT_HEARTBEAT_STALE_SECONDS + 10, workers=None)
    res = await health_service.readiness()
    assert res["status"] == "degraded" and res["ready"] is True
    assert res["checks"]["beat"]["state"] == "stale" and res["checks"]["worker_trading"]["state"] == "missing"
    assert health_service.http_status(res) == 200
    monkeypatch.setattr(settings, "HEALTH_READY_STRICT", True)
    assert health_service.http_status(res) == 503


async def test_bot_ticks_check_only_matters_with_due_bots(db, monkeypatch) -> None:
    async def none(_key):
        return None

    monkeypatch.setattr(health_service, "key_age", none)
    assert (await health_service.check_bot_ticks())["state"] == "idle"

    async def two():
        return 2

    monkeypatch.setattr(health_service, "due_bots", two)
    res = await health_service.check_bot_ticks()
    assert res["ok"] is False and res["state"] == "missing" and res["bots"] == 2


async def test_ready_endpoint_degraded_payload(client, monkeypatch) -> None:
    await _patch_all_ok(monkeypatch, workers=None)
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "degraded"
    monkeypatch.setattr(settings, "HEALTH_READY_STRICT", True)
    assert (await client.get("/health/ready")).status_code == 503


# ------------------------------------------------------------------ docker healthcheck helper
def test_healthcheck_file_mode(tmp_path) -> None:
    f = tmp_path / "hb"
    assert healthcheck.main(["file", str(f), "60"]) == 1  # missing
    heartbeats.touch_file(f)
    assert healthcheck.main(["file", str(f), "60"]) == 0
    old = time.time() - 600
    os.utime(f, (old, old))
    assert healthcheck.main(["file", str(f), "60"]) == 1
    assert healthcheck.main([]) == 2 and healthcheck.main(["bogus"]) == 2


def test_healthcheck_http_mode_unreachable() -> None:
    assert healthcheck.main(["http", "http://127.0.0.1:1/health"]) == 1


# ------------------------------------------------------------------ heartbeats
def test_beat_scheduler_heartbeat_touches_file_and_redis(tmp_path, monkeypatch) -> None:
    path = tmp_path / "beat"
    monkeypatch.setattr(settings, "BEAT_HEARTBEAT_FILE", str(path))
    written: list[str] = []
    monkeypatch.setattr(beat_module, "touch_sync", lambda key, ttl: written.append(key) or True)
    sched = beat_module.HeartbeatScheduler.__new__(beat_module.HeartbeatScheduler)  # no shelve/app needed
    assert sched.heartbeat(now=1000.0) is True
    assert sched.heartbeat(now=1001.0) is False  # throttled
    assert sched.heartbeat(now=1000.0 + beat_module.HEARTBEAT_EVERY) is True
    assert path.exists() and written == [heartbeats.BEAT_KEY, heartbeats.BEAT_KEY]


def test_touch_sync_never_raises_when_redis_is_down() -> None:
    assert heartbeats.touch_sync("heartbeat:test", 10) is False  # tests point Redis at an unreachable port


def test_age_from_raw() -> None:
    assert heartbeats.age_from_raw(None) is None and heartbeats.age_from_raw("x") is None
    assert heartbeats.age_from_raw(str(time.time() - 10)) == pytest.approx(10, abs=1)
    assert heartbeats.age_from_raw(b"100", now=150) == 50


def test_ops_heartbeat_task_writes_queue_key(monkeypatch) -> None:
    keys: list[str] = []
    monkeypatch.setattr(ops_tasks, "touch_sync", lambda key, ttl: keys.append(key) or True)
    assert ops_tasks.heartbeat.run("trading") == {"queue": "trading", "written": True}
    assert keys == ["heartbeat:worker:trading"]


def test_beat_schedule_has_heartbeats_on_both_queues() -> None:
    from app.tasks.celery_app import celery_app

    sched = celery_app.conf.beat_schedule
    assert sched["heartbeat-trading"]["options"]["queue"] == "trading"
    assert sched["heartbeat-default"]["options"]["queue"] == "default"
    assert {"deadman-ping", "retention-cleanup"} <= set(sched)
    assert celery_app.conf.beat_max_loop_interval <= 15


def test_bot_tick_task_records_heartbeat(monkeypatch) -> None:
    from app.tasks import bot_tasks

    keys: list[str] = []
    monkeypatch.setattr(bot_tasks, "touch_sync", lambda key, ttl: keys.append(key) or True)
    monkeypatch.setattr(bot_tasks, "run_async", lambda fn: {"status": "ok"})
    bot_tasks.tick_bot.run("00000000-0000-0000-0000-000000000001")
    monkeypatch.setattr(bot_tasks, "run_async", lambda fn: {"status": "skipped"})
    bot_tasks.tick_bot.run("00000000-0000-0000-0000-000000000001")
    assert keys == [heartbeats.BOT_TICK_KEY]


# ------------------------------------------------------------------ dead man's switch
def test_deadman_ping_skipped_without_url(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEADMAN_PING_URL", "")
    assert "skipped" in ops_tasks.deadman_ping.run()


@pytest.mark.parametrize(("status", "suffix"), [("ready", ""), ("degraded", "/fail"), ("not_ready", "/fail")])
def test_deadman_ping_url_by_status(monkeypatch, status, suffix) -> None:
    monkeypatch.setattr(settings, "DEADMAN_PING_URL", "https://hc-ping.example/uuid/")
    calls: list[tuple[str, bytes]] = []

    def fake_post(url, content=b"", timeout=None):
        calls.append((url, content))
        return httpx.Response(200)

    monkeypatch.setattr(ops_tasks, "run_async", lambda fn: {"status": status, "checks": {"beat": {"ok": False}}})
    monkeypatch.setattr(ops_tasks.httpx, "post", fake_post)
    assert ops_tasks.deadman_ping.run()["http"] == 200
    assert calls[0][0] == "https://hc-ping.example/uuid" + suffix


def test_deadman_ping_network_error_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(settings, "DEADMAN_PING_URL", "https://hc-ping.example/uuid")
    monkeypatch.setattr(ops_tasks, "run_async", lambda fn: {"status": "ready", "checks": {}})

    def boom(*a, **k):
        raise httpx.ConnectError("no dns")

    monkeypatch.setattr(ops_tasks.httpx, "post", boom)
    assert ops_tasks.deadman_ping.run()["error"] == "ConnectError"


# ------------------------------------------------------------------ Alertmanager webhook
AM_PAYLOAD = {
    "version": "4",
    "alerts": [
        {"status": "firing", "labels": {"alertname": "StaleMarketData", "severity": "critical", "job": "streams"},
         "annotations": {"summary": "Preço sem atualização", "description": "há 5 min", "runbook": "docs/RUNBOOK.md"}},
        {"status": "resolved", "labels": {"alertname": "ServiceDown", "severity": "critical"}, "annotations": {}},
    ],
}  # fmt: skip


async def test_alertmanager_webhook_auth_and_forwarding(client, monkeypatch) -> None:
    from app.api.v1.endpoints import monitoring

    url = "/api/v1/monitoring/alertmanager"
    monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_TOKEN", "")
    assert (await client.post(url, json=AM_PAYLOAD)).status_code == 404
    monkeypatch.setattr(settings, "ALERTMANAGER_WEBHOOK_TOKEN", "am-test-token")
    assert (await client.post(url, json=AM_PAYLOAD, headers={"Authorization": "Bearer nope"})).status_code == 401
    sent: list[tuple] = []
    monkeypatch.setattr(monitoring, "emit_alert", lambda event, **kw: sent.append((event, kw)) or True)
    r = await client.post(url, json=AM_PAYLOAD, headers={"Authorization": "Bearer am-test-token"})
    assert r.status_code == 202 and r.json() == {"received": 2}
    firing, resolved = sent[0][1], sent[1][1]
    assert sent[0][0].value == "infra_alert"
    assert firing["severity"] == "critical" and "Preço sem atualização" in firing["text"] and not firing["resolved"]
    assert "Runbook: docs/RUNBOOK.md" in firing["text"]
    assert resolved["severity"] == "info" and resolved["resolved"] is True and firing["dedup"] != resolved["dedup"]
    bad = await client.post(url, content=b"[1]", headers={"Authorization": "Bearer am-test-token"})
    assert bad.status_code == 400


def test_infra_alert_rendering() -> None:
    from app.services.alerts.formatting import render

    title, msg = render("infra_alert", "critical", {"alertname": "ServiceDown", "text": "backend fora"})
    assert title == "Alerta: ServiceDown" and "backend fora" in msg and "🚨" in msg
    title, msg = render("infra_alert", "info", {"alertname": "ServiceDown", "resolved": True})
    assert title == "Resolvido: ServiceDown" and msg.startswith("✅")


# ------------------------------------------------------------------ retention
async def test_retention_cleanup_respects_each_period(db, monkeypatch) -> None:
    user, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    now = datetime.now(UTC)
    old, recent = now - timedelta(days=400), now - timedelta(days=1)
    for i, when in enumerate((old, recent)):
        db.add(RefreshToken(jti=f"j{i}", user_id=user.id, family_id="f", created_at=when, expires_at=when))
        db.add(AlertRecord(event="test", severity="info", status="sent", title="t", message="m", created_at=when))
        db.add(AuditLog(event="x", created_at=when))
        db.add(EquitySnapshot(workspace_id=ws.id, account_id=acc.id, wallet_balance=D("1"), equity=D("1"),
                              taken_at=when))  # fmt: skip
    await db.commit()
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 0)  # kept forever
    deleted = await retention.cleanup(AsyncSessionLocal, now=now)
    assert deleted["refresh_tokens"] == 1 and deleted["alerts"] == 1 and deleted["equity_snapshots"] == 1
    assert "audit_logs" not in deleted and "backtest_runs" not in deleted
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 30)
    assert (await retention.cleanup(AsyncSessionLocal, now=now))["audit_logs"] == 1


# ------------------------------------------------------------------ metrics
async def test_metrics_expose_heartbeat_ages(client, monkeypatch) -> None:
    r = await client.get("/metrics")
    text = r.text
    assert "btp_beat_heartbeat_age_seconds -1.0" in text
    assert 'btp_worker_heartbeat_age_seconds{queue="trading"} -1.0' in text
    assert "btp_bot_last_tick_age_seconds -1.0" in text


def test_rate_limited_counter() -> None:
    guard = RateLimitGuard()
    labels = {"host": "fapi.example", "status": "418"}
    before = REGISTRY.get_sample_value("btp_binance_rate_limited_total", labels) or 0
    guard.record("https://fapi.example", httpx.Response(418, headers={"Retry-After": "5"}))
    assert REGISTRY.get_sample_value("btp_binance_rate_limited_total", labels) == before + 1


# ------------------------------------------------------------------ WebSocket Origin (direct :3000 access)
def test_ws_origin_loopback_allowed_only_outside_production(monkeypatch) -> None:
    from app.api.v1.endpoints.realtime_ws import origin_allowed

    monkeypatch.setattr(settings, "CORS_ORIGINS", ["https://trade.example.com"])
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    assert origin_allowed("https://trade.example.com/")
    assert origin_allowed("http://127.0.0.1:3000") and origin_allowed("http://localhost:13000")
    assert origin_allowed("http://[::1]:3000")
    assert not origin_allowed("http://192.168.0.10:3000") and not origin_allowed("https://evil.example.net")
    assert not origin_allowed("http://localhost.evil.net:3000")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    assert not origin_allowed("http://localhost:3000")
    assert origin_allowed("https://trade.example.com")


async def test_retention_cleanup_continues_after_a_failing_table(db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "AUDIT_RETENTION_DAYS", 30)
    real = retention.delete

    def flaky(model):
        if model is AuditLog:
            raise RuntimeError("append-only")
        return real(model)

    monkeypatch.setattr(retention, "delete", flaky)
    deleted = await retention.cleanup(AsyncSessionLocal)
    assert deleted["audit_logs"] == -1 and deleted["alerts"] == 0
