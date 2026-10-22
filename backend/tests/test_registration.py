"""Registration lockdown (G-03b) and operator bootstrap (G-17)."""

import base64
import os
import secrets

import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.core.security import Role
from app.models import User, Workspace
from app.models.invite import RegistrationInvite

PW = "S3nha-forte-123"


def _prod(**kw) -> Settings:
    base = dict(
        ENVIRONMENT="production", JWT_SECRET=secrets.token_urlsafe(48),
        MASTER_ENCRYPTION_KEY=base64.b64encode(os.urandom(32)).decode(),
        DATABASE_URL="postgresql+asyncpg://u:pw@h/db", COOKIE_SECURE=True, DEBUG=False, REDIS_PASSWORD="pw",
    )  # fmt: skip
    return Settings(**{**base, **kw})


def test_registration_mode_resolution() -> None:
    assert Settings(ALLOW_REGISTRATION=False, REGISTRATION_MODE=None).registration_mode == "closed"
    assert Settings(ALLOW_REGISTRATION=True, REGISTRATION_MODE=None).registration_mode == "open"
    assert Settings(ALLOW_REGISTRATION=True, REGISTRATION_MODE="invite").registration_mode == "invite"


def test_default_is_closed(monkeypatch) -> None:
    monkeypatch.delenv("ALLOW_REGISTRATION", raising=False)
    monkeypatch.delenv("REGISTRATION_MODE", raising=False)
    assert Settings().registration_mode == "closed"


def test_production_refuses_open_registration_without_override() -> None:
    problems = _prod(ALLOW_REGISTRATION=True).production_problems()
    assert any("cadastro aberto" in p for p in problems)
    assert any("cadastro aberto" in p for p in _prod(REGISTRATION_MODE="open").production_problems())
    assert _prod(ALLOW_REGISTRATION=True, ALLOW_OPEN_REGISTRATION_IN_PRODUCTION=True).production_problems() == []
    assert _prod(ALLOW_REGISTRATION=True, REGISTRATION_MODE="invite").production_problems() == []
    assert _prod(ALLOW_REGISTRATION=False).production_problems() == []


def test_startup_blocked_with_open_registration(monkeypatch) -> None:
    from app import main

    good = _prod(ALLOW_REGISTRATION=True)
    fields = ("ENVIRONMENT", "JWT_SECRET", "MASTER_ENCRYPTION_KEY", "DATABASE_URL", "COOKIE_SECURE", "DEBUG")
    for field in (*fields, "REDIS_URL", "CELERY_BROKER_URL"):
        monkeypatch.setattr(main.settings, field, getattr(good, field))
    monkeypatch.setattr(main.settings, "ALLOW_REGISTRATION", True)
    monkeypatch.setattr(main.settings, "REGISTRATION_MODE", None)
    with pytest.raises(RuntimeError, match="cadastro aberto"):
        main.check_production_settings()
    monkeypatch.setattr(main.settings, "REGISTRATION_MODE", "invite")
    main.check_production_settings()


async def _register(client, email: str, token: str | None = None):
    body = {"email": email, "password": PW}
    if token:
        body["invite_token"] = token
    return await client.post("/api/v1/auth/register", json=body)


async def _login(client, email: str) -> dict:
    r = await client.post("/api/v1/auth/login", json={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def test_closed_mode_refuses_signup(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "closed")
    r = await _register(client, "a@example.com")
    assert r.status_code == 403
    assert (await client.get("/api/v1/auth/registration")).json() == {"mode": "closed"}


async def test_production_signup_never_bootstraps_admin(client, db, monkeypatch) -> None:
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "open")
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    r = await _register(client, "first@example.com")
    assert r.status_code == 403 and "create-admin" in r.json()["detail"]
    from app.cli import create_admin

    assert "criado" in await create_admin("Owner@Example.com", "Dono", PW, promote=False)
    r = await _register(client, "second@example.com")
    assert r.status_code == 201 and r.json()["user"]["role"] == "trader"
    owner = await db.scalar(select(User).where(User.email == "owner@example.com"))
    assert owner.role == Role.ADMIN and owner.active_workspace_id
    assert await db.get(Workspace, owner.active_workspace_id)


async def test_invite_only_flow(client, db, monkeypatch) -> None:
    from app.cli import create_admin

    await create_admin("admin@example.com", None, PW, promote=False)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
    monkeypatch.setattr(settings, "TOTP_REQUIRED_FOR_ADMINS", False)  # 2FA is covered by tests/test_mfa.py
    admin = await _login(client, "admin@example.com")

    assert (await _register(client, "x@example.com")).status_code == 403  # no token
    assert (await _register(client, "x@example.com", "forged")).status_code == 403

    r = await client.post("/api/v1/admin/invites", json={"email": "Bound@example.com"}, headers=admin)
    assert r.status_code == 201, r.text
    bound = r.json()
    assert bound["status"] == "pending" and bound["registration_mode"] == "invite"
    assert (await _register(client, "other@example.com", bound["token"])).status_code == 403  # wrong e-mail
    r = await _register(client, "bound@example.com", bound["token"])
    assert r.status_code == 201 and r.json()["user"]["role"] == "trader"
    assert (await _register(client, "bound2@example.com", bound["token"])).status_code == 403  # single use

    from app.core import rate_limiting

    rate_limiting.reset_memory_limiter()  # signup is rate limited per IP
    r = await client.post("/api/v1/admin/invites", json={}, headers=admin)
    free = r.json()
    r = await client.delete(f"/api/v1/admin/invites/{free['id']}", headers=admin)
    assert r.json()["status"] == "revoked"
    assert (await _register(client, "late@example.com", free["token"])).status_code == 403

    listed = {i["id"]: i["status"] for i in (await client.get("/api/v1/admin/invites", headers=admin)).json()}
    assert listed == {bound["id"]: "used", free["id"]: "revoked"}
    stored = await db.scalars(select(RegistrationInvite))
    assert all(inv.token_hash not in (bound["token"], free["token"]) for inv in stored)  # only hashes


async def test_expired_invite_and_non_admin(client, db, monkeypatch) -> None:
    from datetime import timedelta

    from app.cli import create_admin, new_invite
    from app.models.base import utcnow

    await create_admin("admin@example.com", None, PW, promote=False)
    monkeypatch.setattr(settings, "REGISTRATION_MODE", "invite")
    token = (await new_invite(None, 1, "cli")).splitlines()[1]
    inv = await db.scalar(select(RegistrationInvite))
    inv.expires_at = utcnow() - timedelta(minutes=1)
    await db.commit()
    assert (await _register(client, "late@example.com", token)).status_code == 403

    token = (await new_invite(None, None, None)).splitlines()[1]
    assert (await _register(client, "trader@example.com", token)).status_code == 201
    trader = await _login(client, "trader@example.com")
    assert (await client.post("/api/v1/admin/invites", json={}, headers=trader)).status_code == 403


async def test_cli_promote_and_duplicates(db_schema) -> None:
    from app.cli import create_admin, registration_status

    with pytest.raises(SystemExit, match="8 caracteres"):
        await create_admin("a@example.com", None, "short", promote=False)
    with pytest.raises(SystemExit, match="não existe"):
        await create_admin("a@example.com", None, None, promote=True)
    await create_admin("a@example.com", None, PW, promote=False)
    with pytest.raises(SystemExit, match="já existe"):
        await create_admin("a@example.com", None, PW, promote=False)
    assert "super-admins: 1" in await registration_status()


def test_cli_entrypoint_reads_password_from_env(db_schema, monkeypatch, capsys) -> None:
    from app.cli import main

    monkeypatch.setenv("BTP_ADMIN_PASSWORD", PW)
    assert main(["create-admin", "--email", "env@example.com"]) == 0
    assert "Super-admin env@example.com criado" in capsys.readouterr().out
