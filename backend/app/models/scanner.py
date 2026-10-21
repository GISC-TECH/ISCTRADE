"""Market scanner snapshots (latest scans of USDT-M perpetuals)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow


class MarketScan(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "market_scans"

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    testnet: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    interval: Mapped[str] = mapped_column(String(10), nullable=False)
    symbols_scanned: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    results: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list, nullable=False)
    errors: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
