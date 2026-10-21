"""Bot = a strategy configuration bound to an account and symbol."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import BotState

if TYPE_CHECKING:
    from app.models.account import Account


class BotConfig(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "bot_configs"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    strategy_key: Mapped[str] = mapped_column(String(50), nullable=False)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    interval: Mapped[str] = mapped_column(String(10), default="1h", nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    state: Mapped[BotState] = mapped_column(str_enum(BotState), default=BotState.IDLE, nullable=False, index=True)
    # Runtime strategy state persisted between ticks (e.g. grid levels held).
    runtime: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # Bot-level options independent of the strategy (auto symbol selection, news filter, event pause...).
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, server_default="{}", nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    account: Mapped[Account] = relationship(back_populates="bots")
