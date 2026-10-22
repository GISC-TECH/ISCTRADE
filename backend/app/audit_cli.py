"""Audit trail chain CLI.

    python -m app.audit_cli seal                 # seal pending rows now
    python -m app.audit_cli verify [--seal] [--anchor SEQ:HASH]
    python -m app.audit_cli head                 # print the current head (save it outside the server)
    python -m app.audit_cli key-fingerprint      # non-secret id of the HMAC key in use

`verify` prints a JSON report and exits 1 when the chain is broken (cron/monitoring friendly).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.services import audit_chain


async def _run(args: argparse.Namespace) -> int:
    from app.core.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        if args.cmd == "seal":
            print(json.dumps(await audit_chain.seal_pending(db)))
            return 0
        if args.cmd == "head":
            seq, head = await audit_chain.head(db)
            print(json.dumps({"head_seq": seq, "head_hash": head, "key_fingerprint": audit_chain.key_fingerprint()}))
            return 0
        if getattr(args, "seal", False):
            await audit_chain.seal_pending(db)
        anchor = None
        if getattr(args, "anchor", None):
            seq, _, digest = args.anchor.partition(":")
            anchor = (int(seq), digest)
        report = await audit_chain.verify_chain(db, expected_head=anchor)
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        return 0 if report.ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.audit_cli", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("seal")
    sub.add_parser("head")
    sub.add_parser("key-fingerprint")
    v = sub.add_parser("verify")
    v.add_argument("--seal", action="store_true", help="seal pending rows before verifying")
    v.add_argument("--anchor", help="external anchor SEQ:HASH that must match")
    args = parser.parse_args(argv)
    if args.cmd == "key-fingerprint":
        print(audit_chain.key_fingerprint())
        return 0
    return asyncio.run(_run(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
