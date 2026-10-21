"""Single-use refresh tokens: rotation, reuse detection (family revoked), grace window, logout revocation."""

from datetime import timedelta

from sqlalchemy import select, update

from app.core.config import settings
from app.core.security import create_refresh_token
from app.models import RefreshToken, User
from app.models.base import utcnow
from tests.test_api_e2e import register_login

REFRESH = "/api/v1/auth/refresh"


def _refresh_cookie(client) -> str:
    return next(c.value for c in client.cookies.jar if c.name == "refresh_token")


async def _use(client, token: str):
    client.cookies.delete("refresh_token")
    client.cookies.set("refresh_token", token, path="/api/v1/auth")
    return await client.post(REFRESH)


async def _age_all(db) -> None:
    """Pretend every exchange happened long ago (outside the grace window)."""
    await db.execute(
        update(RefreshToken).where(RefreshToken.used_at.is_not(None)).values(used_at=utcnow() - timedelta(minutes=5))
    )
    await db.commit()


async def test_rotation_and_reuse_revokes_family(client, db) -> None:
    await register_login(client, "rot@example.com")
    t1 = _refresh_cookie(client)
    r = await _use(client, t1)
    assert r.status_code == 200
    t2 = _refresh_cookie(client)
    assert t2 != t1
    await _age_all(db)

    stolen = await _use(client, t1)  # replay of an exchanged token -> theft
    assert stolen.status_code == 401
    assert (await _use(client, t2)).status_code == 401  # the legitimate chain is revoked as well
    rows = (await db.scalars(select(RefreshToken))).all()
    assert rows and all(r.revoked_at is not None for r in rows)


async def test_concurrent_refresh_within_grace_is_allowed(client) -> None:
    await register_login(client, "tabs@example.com")
    t1 = _refresh_cookie(client)
    assert (await _use(client, t1)).status_code == 200
    assert (await _use(client, t1)).status_code == 200  # second tab raced with the same cookie


async def test_unknown_or_forged_jti_rejected(client, db) -> None:
    await register_login(client, "forge@example.com")
    user = await db.scalar(select(User).where(User.email == "forge@example.com"))
    fake = create_refresh_token(str(user.id), {"ver": user.token_version, "jti": "0" * 32, "fam": "f" * 32})
    assert (await _use(client, fake)).status_code == 401


async def test_legacy_token_without_family_is_upgraded(client, db) -> None:
    await register_login(client, "legacy@example.com")
    user = await db.scalar(select(User).where(User.email == "legacy@example.com"))
    legacy = create_refresh_token(str(user.id), {"ver": user.token_version})
    assert (await _use(client, legacy)).status_code == 200
    assert (await _use(client, _refresh_cookie(client))).status_code == 200


async def test_logout_revokes_family(client) -> None:
    await register_login(client, "out@example.com")
    t1 = _refresh_cookie(client)
    assert (await client.post("/api/v1/auth/logout")).status_code == 204
    assert (await _use(client, t1)).status_code == 401


def test_grace_setting_is_small() -> None:
    assert 0 <= settings.REFRESH_REUSE_GRACE_SECONDS <= 60
