import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.alerts.email import normalize_email
from app.services.alerts.events import CONFIGURABLE_EVENTS
from app.services.alerts.telegram import normalize_chat_id
from app.services.alerts.whatsapp import normalize_phone

SeverityLiteral = Literal["info", "warning", "critical"]


class AlertEventInfo(BaseModel):
    key: str
    label: str
    severity: SeverityLiteral
    default_enabled: bool


class ChannelStatus(BaseModel):
    channel: str
    enabled: bool
    provider: str
    provider_known: bool
    configured: bool
    default_recipients: int


class AlertSettingsRead(BaseModel):
    enabled: bool
    recipients: list[str]
    telegram_chat_ids: list[str] = []
    emails: list[str] = []
    events: dict[str, bool]
    min_severity: SeverityLiteral
    throttle_minutes: int
    uses_default_recipients: bool
    available_events: list[AlertEventInfo]
    channel: ChannelStatus  # WhatsApp (kept for compatibility)
    channels: list[ChannelStatus] = []  # WhatsApp, Telegram, e-mail


class AlertSettingsUpdate(BaseModel):
    enabled: bool | None = None
    recipients: list[str] | None = Field(None, max_length=10)
    telegram_chat_ids: list[str] | None = Field(None, max_length=10)
    emails: list[str] | None = Field(None, max_length=10)
    events: dict[str, bool] | None = None
    min_severity: SeverityLiteral | None = None
    throttle_minutes: int | None = Field(None, ge=0, le=1440)

    @field_validator("recipients")
    @classmethod
    def _phones(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        out: list[str] = []
        for raw in value:
            phone = normalize_phone(raw)  # ValueError -> 422 with a Portuguese message
            if phone not in out:
                out.append(phone)
        return out

    @field_validator("telegram_chat_ids")
    @classmethod
    def _chats(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(normalize_chat_id(v) for v in value))

    @field_validator("emails")
    @classmethod
    def _emails(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else list(dict.fromkeys(normalize_email(v) for v in value))

    @field_validator("events")
    @classmethod
    def _events(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        if value is None:
            return None
        unknown = sorted(set(value) - set(CONFIGURABLE_EVENTS))
        if unknown:
            raise ValueError(f"eventos desconhecidos: {', '.join(unknown)}")
        return value


class AlertRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    created_at: datetime
    workspace_id: uuid.UUID | None
    account_id: uuid.UUID | None
    event: str
    severity: str
    title: str
    message: str
    status: str
    channel: str
    recipients: list[str]
    attempts: int
    error: str | None
    sent_at: datetime | None


class AlertTestRequest(BaseModel):
    message: str | None = Field(None, max_length=500)


class MonitoringStatus(BaseModel):
    readiness: dict[str, Any]
    channel: ChannelStatus
    channels: list[ChannelStatus] = []
    alerts_24h: int
    alerts_failed_24h: int
    alerts_enabled: bool
    metrics_enabled: bool
