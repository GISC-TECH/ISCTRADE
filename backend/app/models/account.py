"""Binance Futures account connected to a workspace. Credentials stored encrypted (AES-256-GCM)."""
from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.workspace import Workspace


class AccountStatus(str, enum.Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    KILLED = "killed"  # kill switch triggered by risk manager
    ERROR = "error"


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "accounts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_secret: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    is_testnet: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[AccountStatus] = mapped_column(
        SAEnum(AccountStatus, name="account_status"), default=AccountStatus.STOPPED, nullable=False
    )
    # Risk parameters per account
    max_leverage: Mapped[int] = mapped_column(default=5)
    max_risk_per_trade_pct: Mapped[float] = mapped_column(Numeric(6, 3), default=1.0)
    max_daily_loss_pct: Mapped[float] = mapped_column(Numeric(6, 3), default=3.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Numeric(6, 3), default=15.0)

    workspace: Mapped[Workspace] = relationship(back_populates="accounts")
