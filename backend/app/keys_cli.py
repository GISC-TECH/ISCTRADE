"""MASTER_ENCRYPTION_KEY rotation CLI (G-29). Never prints secret values, except `generate`/`audit-pin` output.

    python -m app.keys_cli generate            # print a new random key (base64 of 32 bytes)
    python -m app.keys_cli status              # ciphertexts per key id (exit 1 if any is not on the current key)
    python -m app.keys_cli reencrypt [--dry-run] [--batch-size N]
    python -m app.keys_cli audit-pin [--previous]
        # AUDIT_CHAIN_KEY value equal to the audit-chain key derived from the current (or first previous) master
        # key: pin it BEFORE rotating, otherwise the audit hash chain stops verifying

Procedure: docs/RUNBOOK.md §10 (Rotação de segredos).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys

from app.core import security
from app.core.config import settings


async def _run(args: argparse.Namespace) -> int:
    from app.core.database import AsyncSessionLocal
    from app.services import key_rotation

    async with AsyncSessionLocal() as db:
        report = await key_rotation.run(
            db,
            reencrypt=args.cmd == "reencrypt",
            dry_run=getattr(args, "dry_run", False),
            batch_size=getattr(args, "batch_size", 200),
        )
    data = report.as_dict()
    print(json.dumps(data, indent=2))
    if args.cmd == "status":
        return 0 if data["pending"] == 0 else 1
    return 0 if report.ok else 1


def _audit_pin(previous: bool) -> int:
    source = settings.MASTER_ENCRYPTION_KEY
    if previous:
        keys = [k.strip() for k in settings.MASTER_ENCRYPTION_KEYS_PREVIOUS.split(",") if k.strip()]
        if not keys:
            print("MASTER_ENCRYPTION_KEYS_PREVIOUS está vazio", file=sys.stderr)
            return 2
        source = keys[0]
    if settings.AUDIT_CHAIN_KEY.strip():
        print("AUDIT_CHAIN_KEY já está definido; nada a fazer", file=sys.stderr)
        return 0
    try:
        master = base64.b64decode(source, validate=True)
    except (ValueError, TypeError):
        master = source.encode()
    derived = hmac.new(master, b"isctrade-audit-chain-v1", hashlib.sha256).digest()
    print(f"AUDIT_CHAIN_KEY=b64:{base64.b64encode(derived).decode()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.keys_cli", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate")
    sub.add_parser("status")
    r = sub.add_parser("reencrypt")
    r.add_argument("--dry-run", action="store_true", help="decrypt/encrypt everything but write nothing")
    r.add_argument("--batch-size", type=int, default=200)
    a = sub.add_parser("audit-pin")
    a.add_argument("--previous", action="store_true", help="derive from the first MASTER_ENCRYPTION_KEYS_PREVIOUS")
    args = parser.parse_args(argv)
    if args.cmd == "generate":
        print(base64.b64encode(os.urandom(32)).decode())
        return 0
    if args.cmd == "audit-pin":
        return _audit_pin(args.previous)
    security.keyring()  # fail fast on a malformed key
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
