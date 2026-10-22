"""Telegram and e-mail alert channels (mocked HTTP / fake SMTP) and multi-channel delivery."""

import json
import smtplib
import uuid

import httpx
import pytest

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.alert import AlertRecord, AlertSettings
from app.services.alerts import email as email_mod
from app.services.alerts import service
from app.services.alerts import telegram as tg
from app.services.alerts.notifier import NotifierError
from tests.factories import make_user_workspace
from tests.test_api_e2e import bearer, register_login

BOT_TOKEN = "123456789:" + "A" * 35  # fake, Telegram-shaped
PHONE = "+5511999991234"


def cfg(**kw):
    base = {
        "TELEGRAM_ENABLED": True,
        "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
        "EMAIL_ENABLED": True,
        "SMTP_HOST": "smtp.example.com",
        "SMTP_PORT": 587,
        "EMAIL_FROM": "alertas@example.com",
        "SMTP_USERNAME": "user",
        "SMTP_PASSWORD": "pw-not-real",
    }
    return settings.model_copy(update={**base, **kw})


class TgRecorder:
    def __init__(self, status: int = 200, body: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body if body is not None else {"ok": True, "result": {"message_id": 1}}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.body)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# ------------------------------------------------------------------ Telegram
def test_chat_id_validation_and_mask() -> None:
    assert tg.normalize_chat_id(" 123456789 ") == "123456789"
    assert tg.normalize_chat_id("-1001234567890") == "-1001234567890"
    assert tg.normalize_chat_id("@meu_canal") == "@meu_canal"
    for bad in ("", "abc", "@ab", "12 34", "1" * 25):
        with pytest.raises(ValueError):
            tg.normalize_chat_id(bad)
    assert tg.mask_chat_id("123456789") == "tg:123***89"
    assert tg.mask_chat_id("@meu_canal") == "tg:@me***"


def test_to_html_escapes_and_converts_bold() -> None:
    assert tg.to_html("🚨 *ISCTRADE · Kill* <script> & x") == "🚨 <b>ISCTRADE · Kill</b> &lt;script&gt; &amp; x"


async def test_telegram_send_request_shape() -> None:
    rec = TgRecorder()
    n = tg.build_telegram(cfg(), transport=rec.transport)
    assert n.configured
    await n.send("-1001234567890", "🚨 *Título*\n• a < b")
    req = rec.requests[0]
    assert str(req.url) == f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    body = json.loads(req.content)
    assert body == {
        "chat_id": "-1001234567890",
        "text": "🚨 <b>Título</b>\n• a &lt; b",
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }


@pytest.mark.parametrize(
    "status,body,retryable",
    [
        (403, {"ok": False, "description": "Forbidden: bot was blocked by the user"}, False),
        (400, {"ok": False, "description": "Bad Request: chat not found"}, False),
        (429, {"ok": False, "description": "Too Many Requests: retry after 5"}, True),
        (502, {"ok": False}, True),
        (200, {"ok": False, "description": "weird"}, False),
    ],
)
async def test_telegram_errors(status, body, retryable) -> None:
    n = tg.build_telegram(cfg(), transport=TgRecorder(status, body).transport)
    with pytest.raises(NotifierError) as exc:
        await n.send("123456789", "x")
    assert exc.value.retryable is retryable
    assert BOT_TOKEN not in str(exc.value) and "123456789:" not in str(exc.value)


async def test_telegram_network_error_and_not_configured() -> None:
    def boom(request):
        raise httpx.ConnectError(f"failed for {request.url}")

    n = tg.build_telegram(cfg(), transport=httpx.MockTransport(boom))
    with pytest.raises(NotifierError) as exc:
        await n.send("123456789", "x")
    assert exc.value.retryable and BOT_TOKEN not in str(exc.value)
    off = tg.build_telegram(cfg(TELEGRAM_ENABLED=False))
    assert not off.configured
    with pytest.raises(NotifierError):
        await off.send("123456789", "x")
    assert tg.telegram_status(cfg())["configured"] is True
    assert tg.telegram_status(cfg(TELEGRAM_BOT_TOKEN=""))["configured"] is False


# ------------------------------------------------------------------ e-mail
class FakeSMTP:
    instances: list["FakeSMTP"] = []
    fail: Exception | None = None

    def __init__(self, host, port, timeout=None) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[str] = []
        self.sent: list = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("quit")

    def starttls(self, context=None):
        assert context is not None and context.verify_mode.name == "CERT_REQUIRED"
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append(f"login:{user}")
        if isinstance(FakeSMTP.fail, smtplib.SMTPAuthenticationError):
            raise FakeSMTP.fail

    def send_message(self, msg, from_addr=None, to_addrs=None):
        if FakeSMTP.fail is not None and not isinstance(FakeSMTP.fail, smtplib.SMTPAuthenticationError):
            raise FakeSMTP.fail
        self.sent.append((msg, from_addr, to_addrs))


@pytest.fixture
def fake_smtp():
    FakeSMTP.instances = []
    FakeSMTP.fail = None
    yield FakeSMTP
    FakeSMTP.fail = None


def test_email_validation_mask_and_subject() -> None:
    assert email_mod.normalize_email(" Joao.Silva@Example.COM ") == "Joao.Silva@example.com"
    for bad in ("", "a@", "@b.com", "a b@c.com", "a@b", "x" * 250 + "@b.com"):
        with pytest.raises(ValueError):
            email_mod.normalize_email(bad)
    assert email_mod.mask_email("joao@example.com") == "j***@example.com"
    assert email_mod.split_subject("🚨 *ISCTRADE · Kill*\n• Conta: *A*") == ("🚨 ISCTRADE · Kill", "• Conta: A")


async def test_email_send_starttls_login(fake_smtp) -> None:
    n = email_mod.build_email(cfg(), smtp_factory=fake_smtp)
    assert n.configured
    await n.send("ops@example.com", "⚠️ *ISCTRADE · Bot em erro*\n• Bot: grid")
    smtp = fake_smtp.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.example.com", 587)
    assert smtp.calls == ["starttls", "login:user", "quit"]
    msg, from_addr, to_addrs = smtp.sent[0]
    assert msg["Subject"] == "⚠️ ISCTRADE · Bot em erro" and msg["To"] == "ops@example.com"
    assert from_addr == "alertas@example.com" and to_addrs == ["ops@example.com"]
    assert "• Bot: grid" in msg.get_content() and "*" not in msg.get_content()
    assert msg["Auto-Submitted"] == "auto-generated"


async def test_email_ssl_and_none_modes(fake_smtp) -> None:
    n = email_mod.build_email(cfg(SMTP_SECURITY="none", SMTP_USERNAME=""), smtp_factory=fake_smtp)
    await n.send("ops@example.com", "x")
    assert fake_smtp.instances[0].calls == ["quit"]  # no TLS, no login (trusted relay)
    assert not email_mod.build_email(cfg(SMTP_SECURITY="tls13")).configured
    assert not email_mod.build_email(cfg(EMAIL_FROM="")).configured


@pytest.mark.parametrize(
    "exc,retryable",
    [
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), False),
        (smtplib.SMTPRecipientsRefused({"ops@example.com": (550, b"no such user")}), False),
        (smtplib.SMTPDataError(451, b"try later"), True),
        (smtplib.SMTPDataError(554, b"rejected"), False),
        (smtplib.SMTPServerDisconnected("gone"), True),
        (ConnectionRefusedError(), True),
    ],
)
async def test_email_errors(fake_smtp, exc, retryable) -> None:
    fake_smtp.fail = exc
    n = email_mod.build_email(cfg(), smtp_factory=fake_smtp)
    with pytest.raises(NotifierError) as err:
        await n.send("ops@example.com", "x")
    assert err.value.retryable is retryable
    assert "pw-not-real" not in str(err.value)


# ------------------------------------------------------------------ pipeline
class Fake:
    configured = True

    def __init__(self, channel: str, fail: set[str] | None = None) -> None:
        self.channel = self.name = channel
        self.sent: list[str] = []
        self.fail = fail or set()

    async def send(self, to: str, text: str) -> None:
        if to in self.fail:
            raise NotifierError("HTTP 500", retryable=True)
        self.sent.append(to)


async def test_multichannel_delivery_and_retry(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    db.add(
        AlertSettings(
            workspace_id=ws.id,
            enabled=True,
            recipients=[PHONE],
            telegram_chat_ids=["123456789"],
            emails=[],
            events={},
            min_severity="info",
        )  # fmt: skip
    )
    await db.commit()
    monkeypatch.setattr(settings, "EMAIL_TO", ["ops@example.com"])  # workspace has none -> server default
    wa, tgf, em = Fake("whatsapp"), Fake("telegram", fail={"123456789"}), Fake("email")
    monkeypatch.setattr(service, "build_notifier", lambda: wa)
    monkeypatch.setattr(service, "build_telegram", lambda: tgf)
    monkeypatch.setattr(service, "build_email", lambda: em)

    res = await service.process_alert(AsyncSessionLocal, service.build_payload("bot_error", workspace_id=ws.id))
    assert res["status"] == "pending"
    with pytest.raises(service.DeliveryError):
        await service.deliver_alert(AsyncSessionLocal, res["alert_id"], final=False)
    async with AsyncSessionLocal() as s:
        rec = await s.get(AlertRecord, uuid.UUID(res["alert_id"]))
        assert rec.status == "partial" and rec.channel == "whatsapp,telegram,email"
        assert rec.recipients == ["+5511*****1234", "tg:123***89", "o***@example.com"]
        assert "tg:123***89" in rec.error and "123456789" not in rec.error
    tgf.fail.clear()
    out = await service.deliver_alert(AsyncSessionLocal, res["alert_id"], final=True)
    assert out["status"] == "sent"
    assert wa.sent == [PHONE] and tgf.sent == ["123456789"] and em.sent == ["ops@example.com"]


async def test_only_telegram_configured(db, monkeypatch) -> None:
    from app.services.alerts.notifier import NullNotifier

    _, ws = await make_user_workspace(db)
    await db.commit()
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_IDS", ["@canal_ops", "bad id"])
    tgf = Fake("telegram")
    monkeypatch.setattr(service, "build_notifier", lambda: NullNotifier())
    monkeypatch.setattr(service, "build_telegram", lambda: tgf)
    res = await service.process_alert(AsyncSessionLocal, service.build_payload("bot_error", workspace_id=ws.id))
    out = await service.deliver_alert(AsyncSessionLocal, res["alert_id"])
    assert out["status"] == "sent" and tgf.sent == ["@canal_ops"]


async def test_global_targets_include_opted_in_workspaces(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    db.add(
        AlertSettings(
            workspace_id=ws.id,
            enabled=True,
            recipients=[],
            telegram_chat_ids=["-1001234567890"],
            emails=["a@example.com"],
            events={},
            min_severity="info",
        )  # fmt: skip
    )
    await db.commit()
    monkeypatch.setattr(settings, "TELEGRAM_CHAT_IDS", ["111111"])
    targets = await service.resolve_targets(db, None, "kill_switch_global")
    assert targets["telegram"] == ["111111", "-1001234567890"] and targets["email"] == ["a@example.com"]


async def test_settings_api_telegram_and_email(client) -> None:
    h = bearer(await register_login(client, "channels@example.com"))
    r = await client.put("/api/v1/alerts/settings", json={"telegram_chat_ids": ["nope!"]}, headers=h)
    assert r.status_code == 422
    r = await client.put("/api/v1/alerts/settings", json={"emails": ["not-an-email"]}, headers=h)
    assert r.status_code == 422
    r = await client.put(
        "/api/v1/alerts/settings",
        json={"telegram_chat_ids": ["123456789", "123456789"], "emails": ["Ops@Example.com"]},
        headers=h,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["telegram_chat_ids"] == ["123456789"] and body["emails"] == ["Ops@example.com"]
    assert [c["channel"] for c in body["channels"]] == ["whatsapp", "telegram", "email"]
    assert not any(c["configured"] for c in body["channels"])
    st = (await client.get("/api/v1/monitoring/status", headers=h)).json()
    assert [c["channel"] for c in st["channels"]] == ["whatsapp", "telegram", "email"]
    r = await client.post("/api/v1/alerts/test", headers=h)
    assert r.json()["status"] == "disabled" and r.json()["channel"] == "none"
