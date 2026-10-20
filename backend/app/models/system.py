from __future__ import annotations

from typing import Any

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

GLOBAL_KILL_SWITCH = "global_kill_switch"


class SystemSetting(TimestampMixin, Base):
    """Key/value platform settings (e.g. global kill switch)."""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
