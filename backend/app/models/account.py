"""Exchange account (Binance USDT-M Futures by default) connected to a workspace.

Credentials stored encrypted (AES-256-GCM). ``exchange`` selects the adapter (``app.exchanges.registry``).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum
from app.models.enums import AccountStatus, ExecutionMode

if TYPE_CHECKING:
    from app.models.bot import BotConfig
    from app.models.workspace import Workspace


class Account(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "accounts"

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    exchange: Mapped[str] = mapped_column(
        String(32), default="binance_usdm", server_default="binance_usdm", nullable=False
    )
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_secret: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_last4: Mapped[str] = mapped_column(String(4), nullable=False)
    is_testnet: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    execution_mode: Mapped[ExecutionMode] = mapped_column(
        str_enum(ExecutionMode), default=ExecutionMode.PAPER, server_default=ExecutionMode.PAPER.value, nullable=False
    )
    status: Mapped[AccountStatus] = mapped_column(
        str_enum(AccountStatus), default=AccountStatus.STOPPED, nullable=False
    )
    status_reason: Mapped[str | None] = mapped_column(String(255))

    # Risk parameters per account
    max_leverage: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    max_risk_per_trade_pct: Mapped[Decimal] = mapped_column(default=Decimal("1"), nullable=False)
    max_daily_loss_pct: Mapped[Decimal] = mapped_column(default=Decimal("3"), nullable=False)
    max_drawdown_pct: Mapped[Decimal] = mapped_column(default=Decimal("15"), nullable=False)

    # Paper trading ledger (USDT)
    paper_initial_balance: Mapped[Decimal] = mapped_column(default=Decimal("10000"), nullable=False)
    paper_balance: Mapped[Decimal] = mapped_column(default=Decimal("10000"), nullable=False)

    # Risk state (updated by equity snapshots / daily reset)
    peak_equity: Mapped[Decimal | None] = mapped_column()
    day_start_equity: Mapped[Decimal | None] = mapped_column()
    day_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_equity: Mapped[Decimal | None] = mapped_column()
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    workspace: Mapped[Workspace] = relationship(back_populates="accounts")
    bots: Mapped[list[BotConfig]] = relationship(back_populates="account", cascade="all, delete-orphan")
