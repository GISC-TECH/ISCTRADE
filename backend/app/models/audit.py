"""Append-only audit trail. Never store secrets in `data`."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_workspace_created", "workspace_id", "created_at"),)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info", nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))
    # indexed: account filter in the audit view and ON DELETE SET NULL when an account is removed
    account_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
