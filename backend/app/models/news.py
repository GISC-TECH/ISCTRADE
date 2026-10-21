"""News items (with LLM/lexicon sentiment) and economic calendar events. Global data, not per workspace."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow


class NewsItem(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "news_items"

    source: Mapped[str] = mapped_column(String(50), nullable=False, index=True)  # rss | cryptopanic | x
    source_name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    external_id: Mapped[str | None] = mapped_column(String(255))
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    url: Mapped[str | None] = mapped_column(String(1000))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    assets: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    symbols: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    sentiment: Mapped[float | None] = mapped_column(Float)  # -1 .. 1
    impact: Mapped[str | None] = mapped_column(String(10))  # low | medium | high
    summary_pt: Mapped[str | None] = mapped_column(String(600))
    analysis_method: Mapped[str | None] = mapped_column(String(20))  # llm | lexicon
    analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class EconomicEvent(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "economic_events"
    __table_args__ = (UniqueConstraint("dedup_key", name="uq_economic_events_dedup_key"),)

    source: Mapped[str] = mapped_column(String(30), nullable=False)  # forexfactory | manual
    dedup_key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    country: Mapped[str] = mapped_column(String(10), nullable=False, default="")
    impact: Mapped[str] = mapped_column(String(10), nullable=False, default="low")  # low | medium | high | holiday
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    forecast: Mapped[str | None] = mapped_column(String(50))
    previous: Mapped[str | None] = mapped_column(String(50))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
