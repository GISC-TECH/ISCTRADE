"""Tamper-evident hash chain over `audit_logs` (G-26 / F-02).

Design
------
* `log_event` inserts rows *unsealed* inside the caller's transaction: no lock, no extra query on hot paths
  (bot ticks, orders).
* `seal_pending` (Celery beat `audit.seal`, every AUDIT_SEAL_SECONDS) takes the unsealed rows in
  `(created_at, id)` order and gives each one a gap-free `chain_seq`, the previous row's hash as `prev_hash`
  and ``row_hash = HMAC-SHA256(key, "v1|" + prev_hash + "|" + canonical_json(row))``. Sealers are serialized
  by a PostgreSQL advisory transaction lock, and `chain_seq` is UNIQUE, so the chain can never fork.
* On PostgreSQL a trigger (migration 0009) rejects any UPDATE of a sealed row.
* `verify_chain` recomputes every hash and checks sequence continuity and linkage.

The key is secret (not in the database): someone with write access to the database alone cannot rebuild a
valid chain after editing/deleting rows. It is `AUDIT_CHAIN_KEY` when set, otherwise derived from
`MASTER_ENCRYPTION_KEY`. Rotating the master key therefore requires setting `AUDIT_CHAIN_KEY` to the old
derived value first (`python -m app.audit_cli key-fingerprint` helps to confirm the key in use).

Rows newer than the last seal (seconds) are not yet protected; deleting a *prefix* (retention) is reported as
`truncated_prefix`, never silently accepted. See docs/SECURITY.md §Trilha de auditoria.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import AuditLog
from app.models.base import as_utc

logger = logging.getLogger("audit.chain")

GENESIS_HASH = "0" * 64
CHAIN_VERSION = "v1"
# pg_advisory_xact_lock key ("audit" in ASCII); only sealers take it.
_SEAL_LOCK_KEY = 0x6175646974


def chain_key() -> bytes:
    explicit = (settings.AUDIT_CHAIN_KEY or "").strip()
    if explicit.startswith("b64:"):  # pinned binary key (python -m app.keys_cli audit-pin), survives key rotation
        return base64.b64decode(explicit[4:], validate=True)
    if explicit:
        return explicit.encode()
    try:
        master = base64.b64decode(settings.MASTER_ENCRYPTION_KEY, validate=True)
    except (ValueError, TypeError):
        master = settings.MASTER_ENCRYPTION_KEY.encode()
    return hmac.new(master, b"isctrade-audit-chain-v1", hashlib.sha256).digest()


def key_fingerprint() -> str:
    """Non-secret identifier of the key in use (first 16 hex chars of SHA-256(key))."""
    return hashlib.sha256(chain_key()).hexdigest()[:16]


def _iso(dt: datetime | None) -> str | None:
    dt = as_utc(dt)
    return dt.isoformat(timespec="microseconds") if dt else None


def _uuid(value: uuid.UUID | str | None) -> str | None:
    return str(value) if value is not None else None


def canonical(row: AuditLog) -> str:
    """Stable serialization of every audited field (independent of dialect and dict ordering)."""
    payload = {
        "seq": row.chain_seq,
        "id": _uuid(row.id),
        "created_at": _iso(row.created_at),
        "event": row.event,
        "level": row.level,
        "user_id": _uuid(row.user_id),
        "workspace_id": _uuid(row.workspace_id),
        "account_id": _uuid(row.account_id),
        "ip_address": row.ip_address,
        "data": row.data or {},
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def compute_hash(row: AuditLog, prev_hash: str, key: bytes | None = None) -> str:
    msg = f"{CHAIN_VERSION}|{prev_hash}|{canonical(row)}".encode()
    return hmac.new(key or chain_key(), msg, hashlib.sha256).hexdigest()


async def head(db: AsyncSession) -> tuple[int, str]:
    row = (
        await db.execute(
            select(AuditLog.chain_seq, AuditLog.row_hash)
            .where(AuditLog.chain_seq.is_not(None))
            .order_by(AuditLog.chain_seq.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        return 0, GENESIS_HASH
    return int(row[0]), str(row[1])


async def seal_pending(db: AsyncSession, *, batch: int = 1000, max_batches: int = 50) -> dict[str, Any]:
    """Seal unsealed rows in order. Commits once per batch. Returns {"sealed", "head_seq", "head_hash"}."""
    key = chain_key()
    sealed = 0
    seq, prev = 0, GENESIS_HASH
    for _ in range(max_batches):
        if db.bind is not None and db.bind.dialect.name == "postgresql":
            await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _SEAL_LOCK_KEY})
        seq, prev = await head(db)
        rows = (
            await db.scalars(
                select(AuditLog)
                .where(AuditLog.chain_seq.is_(None))
                .order_by(AuditLog.created_at, AuditLog.id)
                .limit(batch)
            )
        ).all()
        if not rows:
            await db.commit()  # releases the advisory lock
            break
        for row in rows:
            seq += 1
            row.chain_seq = seq
            row.prev_hash = prev
            row.row_hash = compute_hash(row, prev, key)
            prev = row.row_hash
        await db.commit()
        sealed += len(rows)
        if len(rows) < batch:
            break
    if sealed:
        # The head hash in the (shipped) logs is an external anchor: a rebuilt chain would not match it.
        logger.info("audit chain sealed=%s head_seq=%s head_hash=%s", sealed, seq, prev)
    return {"sealed": sealed, "head_seq": seq, "head_hash": prev}


@dataclass
class ChainReport:
    ok: bool = True
    checked: int = 0
    first_seq: int | None = None
    last_seq: int | None = None
    head_hash: str | None = None
    unsealed: int = 0
    truncated_prefix: bool = False
    key_fingerprint: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


MAX_REPORTED_ERRORS = 50


async def verify_chain(
    db: AsyncSession,
    *,
    expected_head: tuple[int, str] | None = None,
    batch: int = 2000,
) -> ChainReport:
    """Recompute and check the whole chain. `expected_head=(seq, hash)` checks against an external anchor."""
    key = chain_key()
    report = ChainReport(key_fingerprint=key_fingerprint())
    report.unsealed = int(
        await db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.chain_seq.is_(None))) or 0
    )

    def fail(seq: int | None, problem: str, **extra: Any) -> None:
        report.ok = False
        if len(report.errors) < MAX_REPORTED_ERRORS:
            report.errors.append({"seq": seq, "problem": problem, **extra})

    prev_seq: int | None = None
    prev_hash: str | None = None
    after = 0
    anchor_seen = False
    while True:
        rows = (
            await db.scalars(
                select(AuditLog)
                .where(AuditLog.chain_seq.is_not(None), AuditLog.chain_seq > after)
                .order_by(AuditLog.chain_seq)
                .limit(batch)
            )
        ).all()
        if not rows:
            break
        for row in rows:
            seq = int(row.chain_seq or 0)
            if prev_seq is None:
                report.first_seq = seq
                if seq != 1:
                    # Older rows were removed (retention or deletion). The first remaining link cannot be checked
                    # against its predecessor; report it so an unexpected truncation is visible.
                    report.truncated_prefix = True
                elif row.prev_hash != GENESIS_HASH:
                    fail(seq, "bad_genesis")
            else:
                if seq != prev_seq + 1:
                    fail(seq, "gap", missing_from=prev_seq + 1, missing_to=seq - 1)
                if row.prev_hash != prev_hash:
                    fail(seq, "broken_link")
            expected = compute_hash(row, row.prev_hash or "", key)
            if not hmac.compare_digest(expected, row.row_hash or ""):
                fail(seq, "hash_mismatch", id=str(row.id), event=row.event)
            if expected_head and seq == expected_head[0]:
                anchor_seen = True
                if row.row_hash != expected_head[1]:
                    fail(seq, "anchor_mismatch")
            report.checked += 1
            prev_seq, prev_hash = seq, row.row_hash
        after = prev_seq or after
        db.expunge_all()  # keep memory flat on large tables
    report.last_seq = prev_seq
    report.head_hash = prev_hash
    if expected_head and not anchor_seen:
        fail(expected_head[0], "anchor_missing")
    return report
