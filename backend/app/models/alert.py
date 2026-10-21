"""Alert settings (per workspace) and alert history."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, utcnow


class AlertSettings(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Per-workspace alert preferences. Missing row = defaults (see app.services.alerts.events)."""

    __tablename__ = "alert_settings"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # WhatsApp recipients in E.164 (e.g. +5511999999999). Empty = WHATSAPP_TO from the server config.
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # event key -> enabled; keys not present use the default of each event.
    events: Mapped[dict[str, bool]] = mapped_column(JSON, default=dict, nullable=False)
    min_severity: Mapped[str] = mapped_column(String(10), default="info", nullable=False)
    throttle_minutes: Mapped[int | None] = mapped_column(Integer)


class AlertRecord(UUIDPrimaryKeyMixin, Base):
    """One alert occurrence and its delivery result."""

    __tablename__ = "alerts"
    __table_args__ = (Index("ix_alerts_workspace_created", "workspace_id", "created_at"),)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    event: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # pending | sent | partial | failed | disabled (no channel configured)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    channel: Mapped[str] = mapped_column(String(30), default="whatsapp", nullable=False)
    recipients: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)  # masked numbers
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(String(500))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
