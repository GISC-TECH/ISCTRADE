from sqlalchemy import select

from app.models import AuditLog
from app.services.audit import log_event


async def test_log_event_persists_and_sanitizes(db) -> None:
    await log_event(db, "account.connected", api_secret="should-not-leak", label="main", commit=True)
    row = (await db.scalars(select(AuditLog))).one()
    assert row.event == "account.connected"
    assert row.data["api_secret"] == "***"
    assert row.data["label"] == "main"
