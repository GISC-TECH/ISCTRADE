"""Alerts: WhatsApp providers (mocked HTTP), formatting, throttle, audit mapping, delivery and API."""

import json
import uuid
from decimal import Decimal as D

import httpx
import pytest

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models import OrderSide
from app.models.alert import AlertRecord, AlertSettings
from app.services.alerts import service, throttle
from app.services.alerts.events import effective_toggles, severity_allows
from app.services.alerts.formatting import render
from app.services.alerts.notifier import NotifierError, NullNotifier
from app.services.alerts.whatsapp import (
    WAGO_DEFAULT_URL,
    build_notifier,
    channel_status,
    normalize_phone,
    render_body_template,
)
from app.services.audit import log_event
from app.services.binance_ws import BinanceStream
from app.services.execution import OrderService, PaperExecutor
from app.services.risk_manager import AccountRiskState
from app.strategy.base import OrderIntent
from app.tasks import signals
from tests.factories import make_account, make_user_workspace
from tests.test_api_e2e import bearer, register_login

FAKE_TOKEN = "T" * 24 + "0123456789abcdef" * 2  # not a real credential
PHONE = "+5511999991234"


def cfg(**kw):
    base = {"WHATSAPP_ENABLED": True, "WHATSAPP_API_TOKEN": FAKE_TOKEN}
    return settings.model_copy(update={**base, **kw})


class Recorder:
    def __init__(self, status: int = 200, body: str = '{"id": "1"}') -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def last_json(self) -> dict:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def captured(monkeypatch):
    """Capture dispatched alert payloads instead of sending them to Celery."""
    out: list[dict] = []
    monkeypatch.setattr(service, "dispatch", lambda payload: out.append(payload) or True)
    return out


@pytest.fixture(autouse=True)
def _reset_throttle():
    throttle.reset_memory()
    yield
    throttle.reset_memory()


# ------------------------------------------------------------------ providers
async def test_wago_hosted_request_format() -> None:
    rec = Recorder(201)
    n = build_notifier(cfg(WHATSAPP_PROVIDER="wago", WHATSAPP_DEVICE_ID="dev123"), transport=rec.transport)
    assert n.name == "wago" and n.configured
    await n.send("+55 (11) 99999-1234", "Olá 🚨")
    req = rec.requests[0]
    assert req.method == "POST" and str(req.url) == f"{WAGO_DEFAULT_URL}/messages"
    assert req.headers["Token"] == FAKE_TOKEN and "authorization" not in req.headers
    assert rec.last_json() == {"phone": PHONE, "message": "Olá 🚨", "device": "dev123"}


async def test_wago_without_device_and_custom_url() -> None:
    rec = Recorder()
    n = build_notifier(
        cfg(WHATSAPP_PROVIDER="wago", WHATSAPP_API_URL="https://wago.example/v1/"), transport=rec.transport
    )
    await n.send(PHONE, "x")
    assert str(rec.requests[0].url) == "https://wago.example/v1/messages"
    assert rec.last_json() == {"phone": PHONE, "message": "x"}


async def test_wago_api_self_hosted_format() -> None:
    rec = Recorder()
    n = build_notifier(
        cfg(WHATSAPP_PROVIDER="wago_api", WHATSAPP_API_URL="http://wago:8080", WHATSAPP_SESSION="main"),
        transport=rec.transport,
    )
    await n.send(PHONE, "oi")
    assert str(rec.requests[0].url) == "http://wago:8080/client/sendMessage/main"
    assert rec.last_json() == {"chatId": "5511999991234@s.whatsapp.net", "text": "oi"}
    assert rec.requests[0].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"


async def test_meta_cloud_format() -> None:
    rec = Recorder()
    n = build_notifier(cfg(WHATSAPP_PROVIDER="meta_cloud", WHATSAPP_PHONE_NUMBER_ID="1098"), transport=rec.transport)
    await n.send(PHONE, "oi")
    assert str(rec.requests[0].url) == "https://graph.facebook.com/v20.0/1098/messages"
    assert rec.requests[0].headers["Authorization"] == f"Bearer {FAKE_TOKEN}"
    body = rec.last_json()
    assert body["messaging_product"] == "whatsapp" and body["type"] == "text"
    assert body["to"] == "5511999991234" and body["text"]["body"] == "oi"


async def test_evolution_format() -> None:
    rec = Recorder()
    n = build_notifier(
        cfg(WHATSAPP_PROVIDER="evolution", WHATSAPP_API_URL="http://evo:8080", WHATSAPP_SESSION="bot"),
        transport=rec.transport,
    )
    await n.send(PHONE, "oi")
    assert str(rec.requests[0].url) == "http://evo:8080/message/sendText/bot"
    assert rec.requests[0].headers["apikey"] == FAKE_TOKEN
    assert rec.last_json() == {"number": "5511999991234", "text": "oi"}


async def test_generic_http_template_escapes_text() -> None:
    rec = Recorder()
    n = build_notifier(
        cfg(
            WHATSAPP_PROVIDER="generic_http",
            WHATSAPP_API_URL="https://gw.example/send",
            WHATSAPP_AUTH_HEADER="X-Api-Key",
            WHATSAPP_AUTH_PREFIX="",
            WHATSAPP_HTTP_METHOD="put",
            WHATSAPP_EXTRA_HEADERS='{"X-Tenant": "t1"}',
            WHATSAPP_BODY_TEMPLATE='{"number": "{to_digits}", "body": {"text": "{text}"}}',
        ),
        transport=rec.transport,
    )
    await n.send(PHONE, 'linha 1\n"aspas"')
    req = rec.requests[0]
    assert req.method == "PUT" and req.headers["X-Api-Key"] == FAKE_TOKEN and req.headers["X-Tenant"] == "t1"
    assert rec.last_json() == {"number": "5511999991234", "body": {"text": 'linha 1\n"aspas"'}}
    with pytest.raises(NotifierError):
        render_body_template("{not json", to=PHONE, text="x")


async def test_provider_errors_are_classified_and_redacted() -> None:
    rec = Recorder(500, body=f"upstream error token={FAKE_TOKEN}")
    n = build_notifier(cfg(), transport=rec.transport)
    with pytest.raises(NotifierError) as err:
        await n.send(PHONE, "x")
    assert err.value.retryable and FAKE_TOKEN not in str(err.value)
    n = build_notifier(cfg(), transport=Recorder(401, "unauthorized").transport)
    with pytest.raises(NotifierError) as err:
        await n.send(PHONE, "x")
    assert not err.value.retryable

    def boom(request):
        raise httpx.ConnectError("down")

    n = build_notifier(cfg(), transport=httpx.MockTransport(boom))
    with pytest.raises(NotifierError) as err:
        await n.send(PHONE, "x")
    assert err.value.retryable


def test_build_notifier_disabled_unknown_and_channel_status() -> None:
    assert isinstance(build_notifier(cfg(WHATSAPP_ENABLED=False)), NullNotifier)
    assert isinstance(build_notifier(cfg(WHATSAPP_PROVIDER="wego")), NullNotifier)  # alias dropped
    assert not build_notifier(cfg(WHATSAPP_API_TOKEN="")).configured
    status = channel_status(cfg())
    assert status == {
        "channel": "whatsapp",
        "enabled": True,
        "provider": "wago",
        "provider_known": True,
        "configured": True,
        "default_recipients": 0,
    }
    assert FAKE_TOKEN not in json.dumps(status)
    assert settings.WHATSAPP_PROVIDER == "wago"


def test_normalize_phone() -> None:
    assert normalize_phone("+55 (11) 99999-1234") == PHONE
    for bad in ("", "123", "+0123456789", "abc"):
        with pytest.raises(ValueError):
            normalize_phone(bad)


# ------------------------------------------------------------------ events / formatting / throttle
def test_toggles_and_severity() -> None:
    t = effective_toggles({"order_filled": True, "unknown": True})
    assert t["order_filled"] is True and "unknown" not in t and t["position_opened"] is False
    assert severity_allows("critical", "warning") and not severity_allows("info", "warning")


def test_render_portuguese_with_emoji_and_redaction() -> None:
    title, msg = render(
        "risk_limit",
        "critical",
        {"account": "Conta A", "reason": "max_daily_loss: 3.2% >= 3%", "error": f"key={FAKE_TOKEN}"},
    )
    assert title == "Limite de drawdown/perda diária atingido"
    assert msg.startswith("🚨 *ISCTRADE") and "• Conta: Conta A" in msg
    assert "limite de perda diária atingido" in msg and FAKE_TOKEN not in msg
    _, num = render("position_closed", "info", {"price": "100.0200", "realized_pnl": "9.95800000000000000000"})
    assert "• Preço: 100.02" in num and "• PnL realizado: 9.958" in num
    _, off = render("kill_switch_global", "info", {"active": False})
    assert off.startswith("✅") and "desativado" in off


async def test_throttle_memory_fallback() -> None:
    assert await throttle.acquire("k1", 15)
    assert not await throttle.acquire("k1", 15)
    assert await throttle.acquire("k2", 15)
    assert await throttle.acquire("k1", 0)  # 0 = no throttle


# ------------------------------------------------------------------ audit mapping + after-commit dispatch
def test_alert_from_audit_mapping() -> None:
    ws, acc = uuid.uuid4(), uuid.uuid4()
    m = lambda e, **d: service.alert_from_audit(e, workspace_id=ws, account_id=acc, data=d)  # noqa: E731
    assert m("risk.kill_switch", reason="max_drawdown")["event"] == "kill_switch_account"
    assert m("order.rejected", reason="max_daily_loss: 3%")["event"] == "risk_limit"
    assert m("order.rejected", reason="leverage_limit: 20 > 5")["event"] == "order_rejected"
    assert m("order.rejected", reason="global_kill_switch") is None
    assert m("order.filled", symbol="BTCUSDT")["event"] == "order_filled"
    assert m("position.stop_loss", symbol="BTCUSDT")["data"]["reason"] == "stop loss"
    assert m("bot.error", bot_id="b", error="boom")["event"] == "bot_error"
    assert m("reconcile.changes", positions_changed=0) is None
    assert m("reconcile.changes", positions_changed=2)["event"] == "reconciler_mismatch"
    g = m("admin.global_kill_switch", active=True)
    assert g["event"] == "kill_switch_global" and g["severity"] == "critical"
    assert m("auth.login") is None


async def test_alerts_dispatch_only_after_commit(db, captured) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    await log_event(
        db, "order.rejected", workspace_id=ws.id, account_id=acc.id, symbol="BTCUSDT", reason="leverage_limit"
    )
    assert captured == []
    await db.commit()
    assert [p["event"] for p in captured] == ["order_rejected"]
    assert captured[0]["account_id"] == str(acc.id)

    await log_event(db, "risk.kill_switch", workspace_id=ws.id, account_id=acc.id, reason="x")
    await db.rollback()
    await db.commit()
    assert len(captured) == 1  # rolled-back alert is discarded


async def test_position_opened_and_closed_events(db, captured) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    e = D("10000")
    state = AccountRiskState(equity=e, peak_equity=e, day_start_equity=e, max_leverage=5)
    svc = OrderService(PaperExecutor())
    await svc.submit(db, acc, OrderIntent(OrderSide.BUY, D("1"), stop_loss=D("95")), symbol="BTCUSDT",
                     ref_price=D("100"), leverage=2, risk_state=state)  # fmt: skip
    await svc.submit(db, acc, OrderIntent(OrderSide.SELL, D("1"), reduce_only=True), symbol="BTCUSDT",
                     ref_price=D("110"), leverage=2, risk_state=state)  # fmt: skip
    await db.commit()
    events = [p["event"] for p in captured]
    assert events == ["order_filled", "position_opened", "order_filled", "position_closed"]
    closed = captured[-1]["data"]
    assert closed["side"] == "LONG" and float(closed["realized_pnl"]) > 0


def test_task_failure_and_ws_hooks_emit(captured) -> None:
    signals.on_task_failure("bots.tick", "t-1", RuntimeError("boom"))
    signals.on_task_failure("alerts.deliver", "t-2", RuntimeError("boom"))  # no alert loops
    assert [p["event"] for p in captured] == ["task_failed"]
    assert captured[0]["data"]["task"] == "bots.tick"


async def test_ws_down_hook_emits(captured) -> None:
    s = BinanceStream(lambda d: None, name="market-testnet")
    await s.on_down(130)
    await s.on_recovered(140)
    assert [p["event"] for p in captured] == ["ws_disconnected", "ws_disconnected"]
    assert captured[0]["severity"] == "warning" and captured[1]["data"]["recovered"] is True


async def test_dispatch_is_noop_without_celery() -> None:
    assert service.dispatch(service.build_payload("test")) is False  # CELERY_DISPATCH_ENABLED=false in tests


# ------------------------------------------------------------------ worker-side processing
class FakeNotifier:
    channel = "whatsapp"
    name = "fake"
    configured = True

    def __init__(self, fail: set[str] | None = None, retryable: bool = True) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail or set()
        self.retryable = retryable

    async def send(self, to: str, text: str) -> None:
        if to in self.fail:
            raise NotifierError("HTTP 500", retryable=self.retryable)
        self.sent.append((to, text))


async def test_process_and_deliver(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws, label="Conta Principal")
    db.add(AlertSettings(workspace_id=ws.id, enabled=True, recipients=[PHONE, "+5521988887777"], events={},
                         min_severity="warning", throttle_minutes=10))  # fmt: skip
    await db.commit()
    fake = FakeNotifier(fail={"+5521988887777"})
    monkeypatch.setattr(service, "build_notifier", lambda: fake)

    payload = service.build_payload("risk_limit", workspace_id=ws.id, account_id=acc.id, dedup="d1",
                                    reason="max_daily_loss: 3.5%")  # fmt: skip
    res = await service.process_alert(AsyncSessionLocal, payload)
    assert res["status"] == "pending"
    # same dedup key inside the window -> throttled
    assert (await service.process_alert(AsyncSessionLocal, payload))["reason"] == "throttled"
    # below min severity / disabled event
    info = service.build_payload("order_rejected", workspace_id=ws.id, severity="info")
    assert (await service.process_alert(AsyncSessionLocal, info))["reason"] == "below_min_severity"
    filled = service.build_payload("order_filled", workspace_id=ws.id, severity="critical")
    assert (await service.process_alert(AsyncSessionLocal, filled))["reason"] == "event_disabled"

    with pytest.raises(service.DeliveryError):
        await service.deliver_alert(AsyncSessionLocal, res["alert_id"], final=False)
    async with AsyncSessionLocal() as s:
        rec = await s.get(AlertRecord, uuid.UUID(res["alert_id"]))
        assert rec.status == "partial" and rec.attempts == 1
        assert rec.recipients == ["+5511*****1234", "+5521*****7777"]
        assert "Conta Principal" in rec.message and PHONE not in (rec.error or "")
    # retry: the already-delivered number is not messaged again
    fake.fail.clear()
    out = await service.deliver_alert(AsyncSessionLocal, res["alert_id"], final=True)
    assert out["status"] == "sent"
    assert [to for to, _ in fake.sent] == [PHONE, "+5521988887777"]


async def test_process_without_channel_records_disabled(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    await db.commit()
    monkeypatch.setattr(service, "build_notifier", lambda: NullNotifier())
    res = await service.process_alert(AsyncSessionLocal, service.build_payload("bot_error", workspace_id=ws.id))
    assert res["status"] == "disabled"


async def test_global_alert_recipients(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    db.add(
        AlertSettings(workspace_id=ws.id, enabled=True, recipients=["+5521988887777"], events={}, min_severity="info")
    )
    await db.commit()
    monkeypatch.setattr(settings, "WHATSAPP_TO", [PHONE, "bad"])
    assert await service.resolve_recipients(db, None, "kill_switch_global") == [PHONE, "+5521988887777"]
    assert await service.resolve_recipients(db, ws.id, "bot_error") == ["+5521988887777"]


async def test_daily_summary_queues_per_workspace(db, captured) -> None:
    _, ws = await make_user_workspace(db)
    await make_account(db, ws, label="A")
    await db.commit()
    out = await service.daily_summary(AsyncSessionLocal)
    assert out == {"workspaces": 1, "queued": 1}
    assert captured[0]["event"] == "daily_summary" and "A: PnL" in captured[0]["data"]["rows"][0]


# ------------------------------------------------------------------ API
async def test_alert_settings_test_and_history_api(client, monkeypatch) -> None:
    h = bearer(await register_login(client, "alerts@example.com"))
    r = await client.get("/api/v1/alerts/settings", headers=h)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["enabled"] and body["recipients"] == [] and body["uses_default_recipients"]
    assert body["events"]["kill_switch_account"] is True and body["events"]["order_filled"] is False
    assert body["channel"]["provider"] == "wago"
    assert {e["key"] for e in body["available_events"]} >= {"kill_switch_global", "daily_summary"}

    r = await client.put("/api/v1/alerts/settings", json={"recipients": ["123"]}, headers=h)
    assert r.status_code == 422
    r = await client.put("/api/v1/alerts/settings", json={"events": {"nope": True}}, headers=h)
    assert r.status_code == 422
    r = await client.put(
        "/api/v1/alerts/settings",
        json={"recipients": ["+55 11 99999-1234"], "events": {"order_filled": True}, "min_severity": "warning",
              "throttle_minutes": 5},
        headers=h,
    )  # fmt: skip
    assert r.status_code == 200, r.text
    assert r.json()["recipients"] == [PHONE] and r.json()["events"]["order_filled"] is True
    assert r.json()["throttle_minutes"] == 5

    rec = Recorder(201)
    wago = build_notifier(cfg(), transport=rec.transport)
    monkeypatch.setattr(service, "build_notifier", lambda: wago)
    r = await client.post("/api/v1/alerts/test", json={}, headers=h)
    assert r.status_code == 200, r.text
    alert = r.json()
    assert alert["status"] == "sent" and alert["event"] == "test" and alert["recipients"] == ["+5511*****1234"]
    assert rec.last_json()["phone"] == PHONE and "alerta de teste" in rec.last_json()["message"].lower()

    r = await client.get("/api/v1/alerts/", headers=h)
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["items"][0]["id"] == alert["id"]

    r = await client.get("/api/v1/monitoring/status", headers=h)
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["alerts_24h"] == 1 and st["channel"]["channel"] == "whatsapp"
    assert st["readiness"]["checks"]["redis"]["ok"] is False  # unreachable in tests
    assert FAKE_TOKEN not in r.text


async def test_test_alert_without_channel_is_recorded_as_disabled(client) -> None:
    h = bearer(await register_login(client, "alerts2@example.com"))
    r = await client.post("/api/v1/alerts/test", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "disabled"
