"""Data retention (daily task ``ops.cleanup``). Every period is configurable; 0 keeps the rows forever."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import settings
from app.models import AlertRecord, AuditLog, BacktestRun, EquitySnapshot, RefreshToken

logger = logging.getLogger(__name__)


def _cutoff(days: int, now: datetime) -> datetime | None:
    return now - timedelta(days=days) if days and days > 0 else None


async def cleanup(sessions: async_sessionmaker[AsyncSession], now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    rules = [
        # refresh tokens: only long after expiry (an expired token can never be exchanged; reuse detection
        # of its family no longer matters once every member is expired)
        ("refresh_tokens", RefreshToken, RefreshToken.expires_at, settings.REFRESH_TOKEN_RETENTION_DAYS),
        ("alerts", AlertRecord, AlertRecord.created_at, settings.ALERTS_RETENTION_DAYS),
        ("audit_logs", AuditLog, AuditLog.created_at, settings.AUDIT_RETENTION_DAYS),
        ("equity_snapshots", EquitySnapshot, EquitySnapshot.taken_at, settings.EQUITY_SNAPSHOT_RETENTION_DAYS),
        ("backtest_runs", BacktestRun, BacktestRun.created_at, settings.BACKTEST_RETENTION_DAYS),
    ]
    deleted: dict[str, int] = {}
    for name, model, column, days in rules:
        cutoff = _cutoff(days, now)
        if cutoff is None:
            continue
        try:
            async with sessions() as db:
                result = await db.execute(delete(model).where(column < cutoff))
                await db.commit()
            deleted[name] = int(result.rowcount or 0)
        except Exception as exc:  # e.g. an append-only trigger on audit_logs: keep cleaning the other tables
            logger.error("retention cleanup failed for %s: %s", name, type(exc).__name__)
            deleted[name] = -1
    logger.info("retention cleanup", extra={"deleted": deleted})
    return deleted
