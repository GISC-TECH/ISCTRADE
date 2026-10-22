"""Custom bot presets saved per workspace (built-in presets live in code: app.strategy.presets)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import JSON, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CustomPreset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A workspace's own bot template. Like built-ins it only pre-fills the bot form: never an execution mode."""

    __tablename__ = "bot_presets"
    __table_args__ = (Index("ix_bot_presets_workspace_name", "workspace_id", "name"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(60), nullable=False)
    interval: Mapped[str] = mapped_column(String(5), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    risk_level: Mapped[str] = mapped_column(String(12), default="moderado", nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Key of the preset it was cloned from (built-in key or "custom:<uuid>"), informational only.
    based_on: Mapped[str | None] = mapped_column(String(80))
