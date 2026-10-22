"""Telegram alert channel (Bot API ``sendMessage``).

``POST {TELEGRAM_API_URL}/bot{TELEGRAM_BOT_TOKEN}/sendMessage`` with ``{"chat_id", "text", "parse_mode": "HTML"}``.
Recipients are chat ids: a user/group id (``123456789``, ``-1001234567890``) or a public channel (``@my_channel``).
The bot must have been started by the user (``/start``) or added to the group/channel.

The token is part of the URL: it is never logged and errors only carry the HTTP status and Telegram's
``description`` (redacted).
"""

from __future__ import annotations

import html
import re
from typing import Any

import httpx

from app.core.config import Settings, settings
from app.core.redaction import redact_text
from app.services.alerts.notifier import NotifierError

MAX_TEXT = 4096
_CHAT_ID = re.compile(r"^(-?\d{1,20}|@[A-Za-z][A-Za-z0-9_]{4,31})$")
_BOLD = re.compile(r"\*([^*\n]+)\*")


def normalize_chat_id(value: str) -> str:
    chat = (value or "").strip()
    if not _CHAT_ID.match(chat):
        raise ValueError("chat id do Telegram inválido (use o número, ex.: 123456789 / -1001234567890, ou @canal)")
    return chat


def mask_chat_id(chat: str) -> str:
    if chat.startswith("@"):
        return f"tg:{chat[:3]}***"
    return f"tg:{chat[:3]}***{chat[-2:]}" if len(chat) > 5 else "tg:***"


def to_html(text: str) -> str:
    """WhatsApp-style markup (``*bold*``) -> Telegram HTML, with everything else escaped."""
    return _BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", html.escape(text, quote=False))


class TelegramNotifier:
    channel = "telegram"
    name = "telegram"

    def __init__(self, cfg: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg or settings
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.cfg.TELEGRAM_ENABLED and self.cfg.TELEGRAM_BOT_TOKEN)

    async def send(self, to: str, text: str) -> None:
        if not self.configured:
            raise NotifierError("telegram is not configured", retryable=False)
        chat = normalize_chat_id(to)
        body: dict[str, Any] = {
            "chat_id": chat,
            "text": to_html(text)[:MAX_TEXT],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        url = f"{self.cfg.TELEGRAM_API_URL.rstrip('/')}/bot{self.cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.cfg.TELEGRAM_TIMEOUT_SECONDS) as c:
                resp = await c.post(url, json=body)
        except httpx.HTTPError as exc:
            raise NotifierError(f"telegram: network error ({type(exc).__name__})") from None
        ok = False
        description = ""
        try:
            data = resp.json()
            ok = bool(data.get("ok"))
            description = str(data.get("description") or "")
        except ValueError:
            pass
        if resp.status_code >= 400 or not ok:
            detail = redact_text(description[:200]).replace(self.cfg.TELEGRAM_BOT_TOKEN, "***")
            # 400 (bad chat id / markup), 401 (bad token), 403 (bot blocked/kicked) will not fix themselves.
            retryable = resp.status_code >= 500 or resp.status_code in (408, 429)
            raise NotifierError(f"telegram: HTTP {resp.status_code} {detail}".strip(), retryable=retryable)


def build_telegram(cfg: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None):
    cfg = cfg or settings
    return TelegramNotifier(cfg, transport=transport)


def telegram_status(cfg: Settings | None = None) -> dict[str, Any]:
    cfg = cfg or settings
    return {
        "channel": "telegram",
        "enabled": cfg.TELEGRAM_ENABLED,
        "provider": "telegram_bot_api",
        "provider_known": True,
        "configured": TelegramNotifier(cfg).configured,
        "default_recipients": len(cfg.TELEGRAM_CHAT_IDS),
    }
