"""Re-encryption of every stored secret under the current MASTER_ENCRYPTION_KEY (G-29).

Encrypted columns and their AAD (the AAD binds a ciphertext to its owner and must match the one used by the code
that encrypts/decrypts it):

* ``accounts.encrypted_api_key`` / ``encrypted_api_secret`` -> ``str(workspace_id)`` (``exchanges/registry.py``)
* ``users.totp_secret_enc`` / ``totp_pending_secret_enc``   -> ``totp:<user id>`` (``services/mfa.py``)

A new encrypted column must be registered in ``ENCRYPTED_FIELDS`` (``tests/test_key_rotation.py`` checks that every
``*_enc``/``encrypted_*`` column of the models is listed).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.models.account import Account
from app.models.user import User


@dataclass(frozen=True)
class EncryptedField:
    model: Any
    columns: tuple[str, ...]
    aad: Callable[[Any], bytes]


ENCRYPTED_FIELDS: tuple[EncryptedField, ...] = (
    EncryptedField(Account, ("encrypted_api_key", "encrypted_api_secret"), lambda a: str(a.workspace_id).encode()),
    EncryptedField(User, ("totp_secret_enc", "totp_pending_secret_enc"), lambda u: f"totp:{u.id}".encode()),
)


@dataclass
class RotationReport:
    current_key_id: str
    by_key: Counter = field(default_factory=Counter)  # "<table>.<column>": {key id | "legacy": n}
    reencrypted: int = 0
    failed: list[str] = field(default_factory=list)  # "<table>.<column>:<row id>: <error>" (never the value)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        keys: dict[str, int] = {}
        for (_, kid), n in self.by_key.items():
            keys[kid] = keys.get(kid, 0) + n
        return {
            "current_key_id": self.current_key_id,
            "keyring": list(security.keyring()),
            "ciphertexts_by_key": keys,
            "pending": sum(n for kid, n in keys.items() if kid != self.current_key_id),
            "reencrypted": self.reencrypted,
            "failed": self.failed,
            "dry_run": self.dry_run,
            "ok": self.ok,
        }


def _kid(token: str) -> str:
    return security.ciphertext_key_id(token) or "legacy"


async def run(db: AsyncSession, *, reencrypt: bool, dry_run: bool = False, batch_size: int = 200) -> RotationReport:
    """Count ciphertexts per key and (when ``reencrypt``) move them all to the current key.

    Each row is decrypted with the matching key of the keyring and verified before being written; commits every
    ``batch_size`` rows, so an interrupted run can simply be repeated (already rotated values are skipped).
    """
    report = RotationReport(current_key_id=security.current_key_id(), dry_run=dry_run)
    pending = 0
    for spec in ENCRYPTED_FIELDS:
        table = spec.model.__tablename__
        rows = (await db.execute(select(spec.model).order_by(spec.model.id))).scalars().all()
        for row in rows:
            aad = spec.aad(row)
            for column in spec.columns:
                token = getattr(row, column)
                if not token:
                    continue
                report.by_key[(f"{table}.{column}", _kid(token))] += 1
                if not reencrypt or not security.needs_reencryption(token):
                    continue
                try:
                    plaintext = security.decrypt_api_key(token, aad)
                    new = security.encrypt_api_key(plaintext, aad)
                    if security.decrypt_api_key(new, aad) != plaintext:  # paranoia: never write an unreadable value
                        raise RuntimeError("round-trip mismatch")
                except Exception as exc:  # noqa: BLE001 - reported per value, the run continues
                    report.failed.append(f"{table}.{column}:{row.id}: {type(exc).__name__}")
                    continue
                report.reencrypted += 1
                if dry_run:
                    continue
                setattr(row, column, new)
                pending += 1
                if pending >= batch_size:
                    await db.commit()
                    pending = 0
    if dry_run:
        await db.rollback()
    else:
        await db.commit()
    return report
