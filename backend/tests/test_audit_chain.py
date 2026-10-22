"""Tamper-evident audit trail: sealing, verification and detection of edits/deletions/re-ordering."""

import json

import pytest
from sqlalchemy import delete, select, update

from app.models import AuditLog
from app.services import audit_chain
from app.services.audit import log_event


async def _events(db, n: int) -> None:
    for i in range(n):
        await log_event(db, "test.event", n=i, price=1.5 + i, nested={"b": [1, 2], "a": "x"})
    await db.commit()


async def test_rows_start_unsealed_and_seal_in_order(db) -> None:
    await _events(db, 5)
    rows = (await db.scalars(select(AuditLog))).all()
    assert all(r.chain_seq is None and r.row_hash is None for r in rows)

    res = await audit_chain.seal_pending(db)
    assert res["sealed"] == 5 and res["head_seq"] == 5
    rows = (await db.scalars(select(AuditLog).order_by(AuditLog.chain_seq))).all()
    assert [r.chain_seq for r in rows] == [1, 2, 3, 4, 5]
    assert rows[0].prev_hash == audit_chain.GENESIS_HASH
    for a, b in zip(rows, rows[1:]):
        assert b.prev_hash == a.row_hash
    assert res["head_hash"] == rows[-1].row_hash

    # idempotent, and new rows extend the chain
    assert (await audit_chain.seal_pending(db))["sealed"] == 0
    await _events(db, 2)
    assert (await audit_chain.seal_pending(db, batch=1))["head_seq"] == 7

    report = await audit_chain.verify_chain(db)
    assert report.ok, report.errors
    assert report.checked == 7 and report.first_seq == 1 and report.last_seq == 7
    assert report.unsealed == 0 and not report.truncated_prefix
    assert report.key_fingerprint == audit_chain.key_fingerprint()


async def test_empty_chain_verifies(db) -> None:
    report = await audit_chain.verify_chain(db)
    assert report.ok and report.checked == 0 and report.head_hash is None


async def test_unsealed_rows_are_reported(db) -> None:
    await _events(db, 2)
    report = await audit_chain.verify_chain(db)
    assert report.ok and report.unsealed == 2


async def _sealed(db, n: int = 6) -> list[AuditLog]:
    await _events(db, n)
    await audit_chain.seal_pending(db)
    return list((await db.scalars(select(AuditLog).order_by(AuditLog.chain_seq))).all())


async def test_detects_edited_data(db) -> None:
    rows = await _sealed(db)
    await db.execute(update(AuditLog).where(AuditLog.id == rows[2].id).values(data={"n": 999}))
    await db.commit()
    db.expire_all()
    report = await audit_chain.verify_chain(db)
    assert not report.ok
    assert report.errors[0] == {"seq": 3, "problem": "hash_mismatch", "id": str(rows[2].id), "event": "test.event"}


@pytest.mark.parametrize("field,value", [("event", "other"), ("level", "critical"), ("ip_address", "1.2.3.4")])
async def test_detects_edited_columns(db, field, value) -> None:
    rows = await _sealed(db, 3)
    await db.execute(update(AuditLog).where(AuditLog.id == rows[1].id).values({field: value}))
    await db.commit()
    db.expire_all()
    report = await audit_chain.verify_chain(db)
    assert [e["problem"] for e in report.errors] == ["hash_mismatch"]


async def test_detects_deleted_row_in_the_middle(db) -> None:
    rows = await _sealed(db)
    await db.execute(delete(AuditLog).where(AuditLog.id == rows[3].id))
    await db.commit()
    report = await audit_chain.verify_chain(db)
    problems = {e["problem"] for e in report.errors}
    assert not report.ok and problems == {"gap", "broken_link"}
    assert report.errors[0]["missing_from"] == 4 and report.errors[0]["missing_to"] == 4


async def test_rehashing_without_the_key_is_detected(db) -> None:
    """An attacker with DB access only can recompute plain SHA-256, not the HMAC."""
    import hashlib

    rows = await _sealed(db, 3)
    target = rows[1]
    target.data = {"n": "forged"}
    forged = hashlib.sha256(f"v1|{target.prev_hash}|{audit_chain.canonical(target)}".encode()).hexdigest()
    await db.execute(update(AuditLog).where(AuditLog.id == target.id).values(data={"n": "forged"}, row_hash=forged))
    await db.commit()
    db.expire_all()
    report = await audit_chain.verify_chain(db)
    assert {e["problem"] for e in report.errors} == {"hash_mismatch", "broken_link"}


async def test_wrong_key_fails_everything(db, monkeypatch) -> None:
    await _sealed(db, 3)
    monkeypatch.setattr(audit_chain.settings, "AUDIT_CHAIN_KEY", "another-key")
    report = await audit_chain.verify_chain(db)
    assert not report.ok and len(report.errors) == 3


async def test_truncated_prefix_is_flagged_but_rest_verifies(db) -> None:
    rows = await _sealed(db)
    await db.execute(delete(AuditLog).where(AuditLog.chain_seq <= 2))
    await db.commit()
    report = await audit_chain.verify_chain(db)
    assert report.ok and report.truncated_prefix and report.first_seq == 3
    assert report.head_hash == rows[-1].row_hash


async def test_anchor_check(db) -> None:
    rows = await _sealed(db, 4)
    ok = await audit_chain.verify_chain(db, expected_head=(4, rows[-1].row_hash))
    assert ok.ok
    bad = await audit_chain.verify_chain(db, expected_head=(4, "f" * 64))
    assert [e["problem"] for e in bad.errors] == ["anchor_mismatch"]
    missing = await audit_chain.verify_chain(db, expected_head=(10, "f" * 64))
    assert [e["problem"] for e in missing.errors] == ["anchor_missing"]


def test_canonical_is_order_independent() -> None:
    a = AuditLog(event="e", level="info", data={"b": 1, "a": 2})
    b = AuditLog(event="e", level="info", data={"a": 2, "b": 1})
    assert audit_chain.canonical(a) == audit_chain.canonical(b)
    assert json.loads(audit_chain.canonical(a))["data"] == {"a": 2, "b": 1}


async def test_admin_verify_endpoint(client, db) -> None:
    from app.api.deps import require_superadmin
    from app.main import app
    from tests.factories import make_user_workspace

    user, _ = await make_user_workspace(db)
    await _events(db, 3)
    app.dependency_overrides[require_superadmin] = lambda: user
    try:
        r = await client.get("/api/v1/admin/audit/verify")
        assert r.status_code == 200 and r.json()["unsealed"] >= 3 and r.json()["checked"] == 0
        r = await client.get("/api/v1/admin/audit/verify", params={"seal": "true"})
        body = r.json()
        assert body["ok"] and body["checked"] >= 3 and body["unsealed"] == 0
        r = await client.get(
            "/api/v1/admin/audit/verify", params={"anchor_seq": body["last_seq"], "anchor_hash": body["head_hash"]}
        )
        assert r.json()["ok"]
        assert (await client.get("/api/v1/admin/audit/verify", params={"anchor_seq": 1})).status_code == 400
        items = (await client.get("/api/v1/admin/audit")).json()["items"]
        assert all(i["chain_seq"] and len(i["row_hash"]) == 64 for i in items)
    finally:
        app.dependency_overrides.pop(require_superadmin, None)


async def test_cli(db, capsys) -> None:
    from app import audit_cli

    await _events(db, 2)
    assert await audit_cli._run(audit_cli.argparse.Namespace(cmd="seal")) == 0
    assert json.loads(capsys.readouterr().out)["sealed"] == 2
    assert await audit_cli._run(audit_cli.argparse.Namespace(cmd="verify", seal=False, anchor=None)) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    await db.execute(update(AuditLog).where(AuditLog.chain_seq == 1).values(event="tampered"))
    await db.commit()
    assert await audit_cli._run(audit_cli.argparse.Namespace(cmd="verify", seal=False, anchor=None)) == 1
    capsys.readouterr()
    assert audit_cli.main(["key-fingerprint"]) == 0
    assert capsys.readouterr().out.strip() == audit_chain.key_fingerprint()


async def test_verify_task_logs_critical_event_on_broken_chain(db) -> None:
    from app.core.database import AsyncSessionLocal
    from app.tasks import audit_tasks

    await _sealed(db, 2)
    await db.execute(update(AuditLog).where(AuditLog.chain_seq == 2).values(level="x"))
    await db.commit()
    report = await audit_tasks._verify(AsyncSessionLocal)
    assert report["ok"] is False
    assert await db.scalar(select(AuditLog).where(AuditLog.event == "audit.chain_broken"))
