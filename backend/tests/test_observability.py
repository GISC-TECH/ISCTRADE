"""Structured logging, redaction, request IDs, readiness, metrics and Celery/WS instrumentation."""

import json
import logging
import uuid
from decimal import Decimal as D
from types import SimpleNamespace

import pytest
from prometheus_client import REGISTRY

from app.core.config import settings
from app.core.logging import JsonFormatter, request_id_var, task_id_var
from app.core.metrics import reason_label
from app.core.redaction import mask_phone, redact, redact_text
from app.models import OrderSide
from app.services import health as health_service
from app.services.binance_ws import BinanceStream
from app.services.execution import OrderService, PaperExecutor
from app.services.risk_manager import AccountRiskState
from app.strategy.base import OrderIntent
from app.tasks import signals
from tests.factories import make_account, make_user_workspace

FAKE_KEY = "A" * 32 + "b" * 32  # looks like a 64-char Binance key (not real)


def _sample(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


# ------------------------------------------------------------------ redaction / logging
def test_redact_text_masks_common_secret_shapes() -> None:
    text = f"Authorization: Bearer abcdef123456 api_key={FAKE_KEY} password: hunter22 key {FAKE_KEY} ok"
    out = redact_text(text)
    assert FAKE_KEY not in out and "abcdef123456" not in out and "hunter22" not in out
    assert out.endswith("ok")
    uid = str(uuid.uuid4())
    assert redact_text(f"account {uid}") == f"account {uid}"  # UUIDs stay readable


def test_redact_nested_structures() -> None:
    out = redact({"api_secret": "x", "nested": {"token": "y", "note": f"k={FAKE_KEY}"}, "items": [D("1.5")]})
    assert out["api_secret"] == "***" and out["nested"]["token"] == "***"
    assert FAKE_KEY not in out["nested"]["note"] and out["items"] == ["1.5"]
    assert mask_phone("+55 11 99999-1234") == "+5511*****1234"


def test_json_formatter_includes_context_and_redacts() -> None:
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "secret=%s done", (FAKE_KEY,), None)
    record.account_id = "acc-1"
    record.api_key = "should-not-appear"
    token = request_id_var.set("req-123")
    try:
        payload = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert payload["level"] == "WARNING" and payload["request_id"] == "req-123"
    assert payload["account_id"] == "acc-1" and payload["api_key"] == "***"
    assert FAKE_KEY not in payload["message"] and payload["timestamp"].endswith("+00:00")


# ------------------------------------------------------------------ request ids
async def test_request_id_generated_and_echoed(client) -> None:
    r = await client.get("/health")
    assert r.status_code == 200 and len(r.headers["x-request-id"]) == 32
    r = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    r = await client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
    assert r.headers["x-request-id"] != "bad id with spaces"


# ------------------------------------------------------------------ readiness
async def test_ready_is_503_when_redis_is_down(client) -> None:
    r = await client.get("/health/ready")  # tests point REDIS_URL/broker at an unreachable port
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready" and body["checks"]["database"]["ok"] is True
    assert body["checks"]["redis"]["ok"] is False


async def test_ready_degraded_without_streams_and_ready_with_heartbeat(client, monkeypatch) -> None:
    async def ok():
        return {"ok": True, "latency_ms": 0.1}

    monkeypatch.setattr(health_service, "check_redis", ok)
    monkeypatch.setattr(health_service, "check_broker", ok)
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "degraded"
    assert r.json()["checks"]["streams"]["state"] == "missing"

    async def fresh():
        return 5.0

    monkeypatch.setattr(health_service, "streams_heartbeat_age", fresh)
    r = await client.get("/health/ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"


# ------------------------------------------------------------------ /metrics
async def test_metrics_endpoint_exposes_http_and_db_gauges(client, db) -> None:
    _, ws = await make_user_workspace(db)
    await make_account(db, ws)
    await db.commit()
    await client.get("/health")
    r = await client.get("/metrics")
    assert r.status_code == 200
    text = r.text
    assert 'btp_http_requests_total{method="GET",route="/health",status="200"}' in text
    assert 'btp_accounts{mode="paper",status="running"} 1.0' in text
    assert "btp_account_equity_usdt{" in text and "btp_global_kill_switch 0.0" in text


async def test_metrics_token_and_disable(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "METRICS_TOKEN", "test-metrics-token")
    assert (await client.get("/metrics")).status_code == 401
    ok = await client.get("/metrics", headers={"Authorization": "Bearer test-metrics-token"})
    assert ok.status_code == 200
    monkeypatch.setattr(settings, "METRICS_ENABLED", False)
    assert (await client.get("/metrics")).status_code == 404


async def test_order_metrics_by_result_and_reason(db) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    e = D("10000")
    state = AccountRiskState(equity=e, peak_equity=e, day_start_equity=e, max_leverage=5)
    svc = OrderService(PaperExecutor())
    filled = {"mode": "paper", "result": "filled", "reason": "none"}
    rejected = {"mode": "paper", "result": "rejected", "reason": "stop_loss_required"}
    before_f, before_r = _sample("btp_orders_total", filled), _sample("btp_orders_total", rejected)
    await svc.submit(db, acc, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("95")), symbol="BTCUSDT",
                     ref_price=D("100"), leverage=2, risk_state=state)  # fmt: skip
    await svc.submit(db, acc, OrderIntent(OrderSide.BUY, D("1")), symbol="BTCUSDT",
                     ref_price=D("100"), leverage=2, risk_state=state)  # fmt: skip
    assert _sample("btp_orders_total", filled) == before_f + 1
    assert _sample("btp_orders_total", rejected) == before_r + 1


def test_reason_label_is_bounded() -> None:
    assert reason_label("max_daily_loss: 3.10% >= 3%") == "max_daily_loss"
    assert reason_label("execution_error: Binance said no") == "execution_error"
    assert reason_label(None) == "none"


# ------------------------------------------------------------------ celery signals
def test_celery_request_id_propagation_and_task_metrics() -> None:
    token = request_id_var.set("req-abc")
    headers: dict = {}
    try:
        signals._propagate_request_id(headers=headers)
    finally:
        request_id_var.reset(token)
    assert headers == {"request_id": "req-abc"}

    task = SimpleNamespace(name="tests.fake", request=SimpleNamespace(request_id="req-abc"))
    labels = {"task": "tests.fake", "status": "success"}
    before = _sample("btp_celery_tasks_total", labels)
    signals._task_prerun(task_id="t-1", task=task)
    assert request_id_var.get() == "req-abc" and task_id_var.get() == "t-1"
    signals._task_postrun(task_id="t-1", task=task, state="SUCCESS")
    assert request_id_var.get() is None and task_id_var.get() is None
    assert _sample("btp_celery_tasks_total", labels) == before + 1
    assert _sample("btp_celery_task_duration_seconds_count", {"task": "tests.fake"}) >= 1


# ------------------------------------------------------------------ websocket down/recovered hooks
class _Probe(BinanceStream):
    def __init__(self) -> None:
        super().__init__(lambda m: None, name="probe")
        self.events: list[str] = []

    async def on_down(self, seconds: float) -> None:
        self.events.append("down")

    async def on_recovered(self, seconds: float) -> None:
        self.events.append("up")


@pytest.mark.parametrize("threshold", [0.0])
async def test_ws_down_and_recovered_hooks(threshold: float) -> None:
    s = _Probe()
    s.down_alert_seconds = threshold
    s.connections = 1
    await s._mark_connected()
    s.connections = 2
    await s._mark_disconnected()  # starts the down timer
    await s._mark_disconnected()  # threshold reached -> on_down once
    await s._mark_disconnected()
    assert s.events == ["down"]
    before = _sample("btp_ws_reconnects_total", {"stream": "probe"})
    s.connections = 3
    await s._mark_connected()
    assert s.events == ["down", "up"]
    assert _sample("btp_ws_reconnects_total", {"stream": "probe"}) == before + 1
    assert _sample("btp_ws_connected", {"stream": "probe"}) == 1
