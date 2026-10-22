"""Append-only, tamper-evident audit trail. Never store secrets in `data`.

Rows are inserted unsealed by `log_event` (no contention on the hot path) and then sealed in order by
`app.services.audit_chain.seal_pending`: each sealed row gets a gap-free `chain_seq`, the `prev_hash` of the
previous sealed row and `row_hash = HMAC-SHA256(key, prev_hash || canonical(row))`. Editing, deleting or
re-ordering a sealed row breaks the chain, which `verify_chain` (API `/admin/audit/verify`, CLI
`python -m app.audit_cli verify`) detects.

The user/workspace/account columns are plain UUIDs (no foreign keys since migration 0009): an ON DELETE
CASCADE / SET NULL would silently delete or rewrite audited rows and break the chain.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, DateTime, Index, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UUIDPrimaryKeyMixin, utcnow


class AuditLog(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_workspace_created", "workspace_id", "created_at"),)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    event: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(10), default="info", nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid())
    # indexed: account filter in the audit view
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    # Hash chain (NULL until sealed)
    chain_seq: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    row_hash: Mapped[str | None] = mapped_column(String(64))
