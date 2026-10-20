"""Orders, fills (trades), positions and equity snapshots."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum, utcnow
from app.models.enums import OrderSide, OrderStatus, OrderType


class Order(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_account_created", "account_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_configs.id", ondelete="SET NULL"), index=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide, 4), nullable=False)
    type: Mapped[OrderType] = mapped_column(str_enum(OrderType, 30), nullable=False)
    status: Mapped[OrderStatus] = mapped_column(str_enum(OrderStatus), default=OrderStatus.PENDING, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(nullable=False)
    price: Mapped[Decimal | None] = mapped_column()
    stop_price: Mapped[Decimal | None] = mapped_column()
    stop_loss: Mapped[Decimal | None] = mapped_column()
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    executed_qty: Mapped[Decimal] = mapped_column(default=Decimal("0"), nullable=False)
    avg_price: Mapped[Decimal | None] = mapped_column()
    client_order_id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String(40), index=True)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(255))  # strategy tag or rejection reason


class Trade(UUIDPrimaryKeyMixin, Base):
    """An execution (fill)."""

    __tablename__ = "trades"
    __table_args__ = (Index("ix_trades_account_executed", "account_id", "executed_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id", ondelete="SET NULL"), index=True)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("bot_configs.id", ondelete="SET NULL"))
    symbol: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    side: Mapped[OrderSide] = mapped_column(str_enum(OrderSide, 4), nullable=False)
    price: Mapped[Decimal] = mapped_column(nullable=False)
    quantity: Mapped[Decimal] = mapped_column(nullable=False)
    fee: Mapped[Decimal] = mapped_column(default=Decimal("0"), nullable=False)
    fee_asset: Mapped[str] = mapped_column(String(10), default="USDT", nullable=False)
    realized_pnl: Mapped[Decimal] = mapped_column(default=Decimal("0"), nullable=False)
    exchange_trade_id: Mapped[str | None] = mapped_column(String(40))
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Position(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Net position (one-way mode) per account and symbol. quantity > 0 long, < 0 short."""

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("account_id", "symbol", name="uq_positions_account_symbol"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    symbol: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(default=Decimal("0"), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(default=Decimal("0"), nullable=False)
    mark_price: Mapped[Decimal | None] = mapped_column()
    unrealized_pnl: Mapped[Decimal] = mapped_column(default=Decimal("0"), nullable=False)
    leverage: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    stop_loss: Mapped[Decimal | None] = mapped_column()
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EquitySnapshot(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "equity_snapshots"
    __table_args__ = (Index("ix_equity_account_taken", "account_id", "taken_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id", ondelete="CASCADE"), index=True)
    wallet_balance: Mapped[Decimal] = mapped_column(nullable=False)
    unrealized_pnl: Mapped[Decimal] = mapped_column(default=Decimal("0"), nullable=False)
    equity: Mapped[Decimal] = mapped_column(nullable=False)
    is_paper: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    taken_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
