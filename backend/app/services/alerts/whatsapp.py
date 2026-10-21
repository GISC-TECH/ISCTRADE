"""WhatsApp providers.

* ``wago`` (default): WAGO hosted API (https://wago.hablame.co), Wassenger-compatible.
  ``POST {WHATSAPP_API_URL or https://wago-api.hablame.co/v1}/messages``, header ``Token: <API key>``,
  body ``{"phone": "+5511...", "message": "...", "device": "<optional device id>"}``.
* ``wago_api``: self-hosted wago-api. ``POST {url}/client/sendMessage/{session}`` with
  ``{"chatId": "<digits>@s.whatsapp.net", "text": "..."}``.
* ``meta_cloud``: WhatsApp Cloud API ``POST https://graph.facebook.com/v20.0/{phone_number_id}/messages``.
* ``evolution``: Evolution API ``POST {url}/message/sendText/{instance}`` with header ``apikey``.
* ``generic_http``: any HTTP gateway (URL, method, auth header and JSON body template from env).

No provider ever logs or returns the token; error messages are redacted.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from app.core.config import Settings, settings
from app.core.redaction import redact_text
from app.services.alerts.notifier import Notifier, NotifierError, NullNotifier

WAGO_DEFAULT_URL = "https://wago-api.hablame.co/v1"
META_DEFAULT_URL = "https://graph.facebook.com/v20.0"
PROVIDERS = ("wago", "wago_api", "meta_cloud", "evolution", "generic_http")

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")


def normalize_phone(value: str) -> str:
    """'+55 (11) 99999-1234' -> '+5511999991234'. Raises ValueError when not E.164."""
    digits = re.sub(r"\D", "", value or "")
    phone = f"+{digits}"
    if not _E164.match(phone):
        raise ValueError("número deve estar no formato E.164, ex.: +5511999999999")
    return phone


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone)


class _HttpProvider:
    channel = "whatsapp"
    name = "base"

    def __init__(self, cfg: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.cfg = cfg
        self.transport = transport

    @property
    def configured(self) -> bool:  # pragma: no cover - overridden
        return False

    def _request(self, to: str, text: str) -> tuple[str, str, dict[str, str], Any]:  # pragma: no cover
        raise NotImplementedError

    async def send(self, to: str, text: str) -> None:
        if not self.configured:
            raise NotifierError(f"provider {self.name} is not configured", retryable=False)
        method, url, headers, body = self._request(normalize_phone(to), text)
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=self.cfg.WHATSAPP_TIMEOUT_SECONDS) as client:
                resp = await client.request(method, url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise NotifierError(f"{self.name}: network error ({type(exc).__name__})") from None
        if resp.status_code >= 400:
            detail = redact_text(resp.text[:200]) if resp.text else ""
            # 4xx (except 408/429) are configuration/content problems: retrying will not help.
            retryable = resp.status_code >= 500 or resp.status_code in (408, 429)
            raise NotifierError(f"{self.name}: HTTP {resp.status_code} {detail}".strip(), retryable=retryable)


class WagoNotifier(_HttpProvider):
    """WAGO hosted API (wago.hablame.co)."""

    name = "wago"

    @property
    def configured(self) -> bool:
        return bool(self.cfg.WHATSAPP_API_TOKEN)

    def _request(self, to: str, text: str) -> tuple[str, str, dict[str, str], Any]:
        base = (self.cfg.WHATSAPP_API_URL or WAGO_DEFAULT_URL).rstrip("/")
        body: dict[str, Any] = {"phone": to, "message": text}
        if self.cfg.WHATSAPP_DEVICE_ID:
            body["device"] = self.cfg.WHATSAPP_DEVICE_ID
        headers = {"Token": self.cfg.WHATSAPP_API_TOKEN, "Content-Type": "application/json"}
        return "POST", f"{base}/messages", headers, body


class WagoApiNotifier(_HttpProvider):
    """Self-hosted wago-api (whatsmeow based)."""

    name = "wago_api"

    @property
    def configured(self) -> bool:
        return bool(self.cfg.WHATSAPP_API_URL and self.cfg.WHATSAPP_SESSION)

    def _request(self, to: str, text: str) -> tuple[str, str, dict[str, str], Any]:
        base = self.cfg.WHATSAPP_API_URL.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if self.cfg.WHATSAPP_API_TOKEN:
            headers[self.cfg.WHATSAPP_AUTH_HEADER] = f"{self.cfg.WHATSAPP_AUTH_PREFIX}{self.cfg.WHATSAPP_API_TOKEN}"
        body = {"chatId": f"{_digits(to)}@s.whatsapp.net", "text": text}
        return "POST", f"{base}/client/sendMessage/{self.cfg.WHATSAPP_SESSION}", headers, body


class MetaCloudNotifier(_HttpProvider):
    name = "meta_cloud"

    @property
    def configured(self) -> bool:
        return bool(self.cfg.WHATSAPP_API_TOKEN and self.cfg.WHATSAPP_PHONE_NUMBER_ID)

    def _request(self, to: str, text: str) -> tuple[str, str, dict[str, str], Any]:
        base = (self.cfg.WHATSAPP_API_URL or META_DEFAULT_URL).rstrip("/")
        headers = {"Authorization": f"Bearer {self.cfg.WHATSAPP_API_TOKEN}", "Content-Type": "application/json"}
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": _digits(to),
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        return "POST", f"{base}/{self.cfg.WHATSAPP_PHONE_NUMBER_ID}/messages", headers, body


class EvolutionNotifier(_HttpProvider):
    name = "evolution"

    @property
    def configured(self) -> bool:
        return bool(self.cfg.WHATSAPP_API_URL and self.cfg.WHATSAPP_SESSION and self.cfg.WHATSAPP_API_TOKEN)

    def _request(self, to: str, text: str) -> tuple[str, str, dict[str, str], Any]:
        base = self.cfg.WHATSAPP_API_URL.rstrip("/")
        headers = {"apikey": self.cfg.WHATSAPP_API_TOKEN, "Content-Type": "application/json"}
        body = {"number": _digits(to), "text": text}
        return "POST", f"{base}/message/sendText/{self.cfg.WHATSAPP_SESSION}", headers, body


class GenericHttpNotifier(_HttpProvider):
    name = "generic_http"

    @property
    def configured(self) -> bool:
        return bool(self.cfg.WHATSAPP_API_URL)

    def _request(self, to: str, text: str) -> tuple[str, str, dict[str, str], Any]:
        headers = {"Content-Type": "application/json"}
        if self.cfg.WHATSAPP_EXTRA_HEADERS:
            try:
                extra = json.loads(self.cfg.WHATSAPP_EXTRA_HEADERS)
            except json.JSONDecodeError:
                raise NotifierError("WHATSAPP_EXTRA_HEADERS is not valid JSON", retryable=False) from None
            headers.update({str(k): str(v) for k, v in extra.items()})
        if self.cfg.WHATSAPP_API_TOKEN:
            headers[self.cfg.WHATSAPP_AUTH_HEADER] = f"{self.cfg.WHATSAPP_AUTH_PREFIX}{self.cfg.WHATSAPP_API_TOKEN}"
        body = render_body_template(self.cfg.WHATSAPP_BODY_TEMPLATE, to=to, text=text)
        url = self.cfg.WHATSAPP_API_URL.replace("{to}", _digits(to)).replace("{session}", self.cfg.WHATSAPP_SESSION)
        return self.cfg.WHATSAPP_HTTP_METHOD.upper(), url, headers, body


def render_body_template(template: str, *, to: str, text: str) -> Any:
    """Substitute the {to}, {to_digits} and {text} placeholders inside a JSON body template.

    Values are JSON-escaped, so quotes/newlines in the message cannot break the document.
    """

    def esc(value: str) -> str:
        return json.dumps(value, ensure_ascii=False)[1:-1]

    rendered = template.replace("{to_digits}", esc(_digits(to))).replace("{to}", esc(to)).replace("{text}", esc(text))
    try:
        return json.loads(rendered)
    except json.JSONDecodeError:
        raise NotifierError("WHATSAPP_BODY_TEMPLATE is not valid JSON", retryable=False) from None


_REGISTRY: dict[str, type[_HttpProvider]] = {
    "wago": WagoNotifier,
    "wago_api": WagoApiNotifier,
    "meta_cloud": MetaCloudNotifier,
    "evolution": EvolutionNotifier,
    "generic_http": GenericHttpNotifier,
}


def build_notifier(cfg: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None) -> Notifier:
    cfg = cfg or settings
    if not cfg.WHATSAPP_ENABLED:
        return NullNotifier()
    cls = _REGISTRY.get(cfg.WHATSAPP_PROVIDER.strip().lower())
    if cls is None:
        return NullNotifier()
    return cls(cfg, transport=transport)


def channel_status(cfg: Settings | None = None) -> dict[str, Any]:
    """Channel configuration without secrets (for the API / monitoring page)."""
    cfg = cfg or settings
    provider = cfg.WHATSAPP_PROVIDER.strip().lower()
    notifier = build_notifier(cfg)
    return {
        "channel": "whatsapp",
        "enabled": cfg.WHATSAPP_ENABLED,
        "provider": provider,
        "provider_known": provider in _REGISTRY,
        "configured": bool(notifier.configured),
        "default_recipients": len(cfg.WHATSAPP_TO),
    }
