"""E-mail alert channel (SMTP, standard library only).

``SMTP_SECURITY``: ``starttls`` (default, port 587), ``ssl`` (implicit TLS, port 465) or ``none`` (plain relay on
a trusted network only). TLS always verifies the server certificate. Login is used when ``SMTP_USERNAME`` is set.
The blocking ``smtplib`` call runs in a worker thread so the event loop is never stalled.
"""

from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Any

from app.core.config import Settings, settings
from app.core.redaction import redact_text
from app.services.alerts.notifier import NotifierError

_EMAIL = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$"
)
SECURITY_MODES = ("starttls", "ssl", "none")


def normalize_email(value: str) -> str:
    addr = (value or "").strip()
    if len(addr) > 254 or not _EMAIL.match(addr):
        raise ValueError("e-mail inválido")
    local, _, domain = addr.rpartition("@")
    return f"{local}@{domain.lower()}"


def mask_email(addr: str) -> str:
    local, _, domain = addr.partition("@")
    return f"{local[:1]}***@{domain}"


def split_subject(text: str) -> tuple[str, str]:
    """First line of the rendered alert (``🚨 *ISCTRADE · Title*``) becomes the subject; markup is stripped."""
    plain = text.replace("*", "")
    first, _, rest = plain.partition("\n")
    return first.strip()[:200] or "ISCTRADE", rest.strip() or plain


class EmailNotifier:
    channel = "email"
    name = "smtp"

    def __init__(self, cfg: Settings | None = None, smtp_factory: Any = None) -> None:
        self.cfg = cfg or settings
        self._factory = smtp_factory  # tests inject a fake smtplib.SMTP

    @property
    def configured(self) -> bool:
        c = self.cfg
        return bool(c.EMAIL_ENABLED and c.SMTP_HOST and c.EMAIL_FROM and c.SMTP_SECURITY.lower() in SECURITY_MODES)

    def _message(self, to: str, text: str) -> EmailMessage:
        subject, body = split_subject(text)
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.cfg.EMAIL_FROM
        msg["To"] = to
        msg["Date"] = formatdate(localtime=False)
        msg["Message-ID"] = make_msgid(domain=self.cfg.EMAIL_FROM.rpartition("@")[2] or None)
        msg["Auto-Submitted"] = "auto-generated"
        msg.set_content(body + "\n\n— ISCTRADE (mensagem automática)\n")
        return msg

    def _send_sync(self, to: str, msg: EmailMessage) -> None:
        c = self.cfg
        mode = c.SMTP_SECURITY.lower()
        context = ssl.create_default_context()
        if self._factory is not None:
            smtp = self._factory(c.SMTP_HOST, c.SMTP_PORT, timeout=c.SMTP_TIMEOUT_SECONDS)
        elif mode == "ssl":
            smtp = smtplib.SMTP_SSL(c.SMTP_HOST, c.SMTP_PORT, timeout=c.SMTP_TIMEOUT_SECONDS, context=context)
        else:
            smtp = smtplib.SMTP(c.SMTP_HOST, c.SMTP_PORT, timeout=c.SMTP_TIMEOUT_SECONDS)
        with smtp:
            if mode == "starttls":
                smtp.starttls(context=context)
            if c.SMTP_USERNAME:
                smtp.login(c.SMTP_USERNAME, c.SMTP_PASSWORD)
            smtp.send_message(msg, from_addr=c.EMAIL_FROM, to_addrs=[to])

    async def send(self, to: str, text: str) -> None:
        if not self.configured:
            raise NotifierError("e-mail is not configured", retryable=False)
        addr = normalize_email(to)
        msg = self._message(addr, text)
        try:
            await asyncio.to_thread(self._send_sync, addr, msg)
        except smtplib.SMTPAuthenticationError:
            raise NotifierError("smtp: authentication failed", retryable=False) from None
        except smtplib.SMTPRecipientsRefused:
            raise NotifierError("smtp: recipient refused", retryable=False) from None
        except smtplib.SMTPResponseException as exc:
            retryable = 400 <= exc.smtp_code < 500  # 4xx = temporary, 5xx = permanent
            detail = redact_text(str(exc.smtp_error)[:120])
            raise NotifierError(f"smtp: {exc.smtp_code} {detail}", retryable=retryable) from None
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifierError(f"smtp: {type(exc).__name__}") from None


def build_email(cfg: Settings | None = None, smtp_factory: Any = None) -> EmailNotifier:
    return EmailNotifier(cfg or settings, smtp_factory=smtp_factory)


def email_status(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or settings
    return {
        "channel": "email",
        "enabled": cfg.EMAIL_ENABLED,
        "provider": "smtp",
        "provider_known": cfg.SMTP_SECURITY.lower() in SECURITY_MODES,
        "configured": EmailNotifier(cfg).configured,
        "default_recipients": len(cfg.EMAIL_TO),
    }
