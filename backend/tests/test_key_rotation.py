"""MASTER_ENCRYPTION_KEY keyring and re-encryption (G-29) + Redis auth injection (G-27)."""

from __future__ import annotations

import base64
import os

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import select

from app.core import security
from app.core.config import Settings, settings, with_redis_auth
from app.exchanges.registry import adapter_for_account
from app.models import Account, User
from app.services import audit_chain, key_rotation, mfa
from tests.factories import make_account, make_user_workspace


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def _legacy_encrypt(key_b64: str, plaintext: str, aad: bytes) -> str:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    nonce = os.urandom(12)
    return base64.b64encode(nonce + AESGCM(base64.b64decode(key_b64)).encrypt(nonce, plaintext.encode(), aad)).decode()


@pytest.fixture
def rotate(monkeypatch):
    """Switch to a new current key keeping the old one as previous; returns (old, new)."""

    def _rotate():
        old, new = settings.MASTER_ENCRYPTION_KEY, _key()
        monkeypatch.setattr(settings, "MASTER_ENCRYPTION_KEY", new)
        monkeypatch.setattr(settings, "MASTER_ENCRYPTION_KEYS_PREVIOUS", old)
        return old, new

    return _rotate


def test_ciphertext_carries_key_id_and_is_tenant_bound() -> None:
    token = security.encrypt_api_key("s3cret", b"ws-1")
    assert token.startswith(f"v2:{security.current_key_id()}:")
    assert security.decrypt_api_key(token, b"ws-1") == "s3cret"
    assert not security.needs_reencryption(token)
    with pytest.raises(InvalidTag):
        security.decrypt_api_key(token, b"ws-2")


def test_legacy_ciphertext_still_decrypts_and_needs_reencryption() -> None:
    legacy = _legacy_encrypt(settings.MASTER_ENCRYPTION_KEY, "old-format", b"aad")
    assert security.ciphertext_key_id(legacy) is None
    assert security.decrypt_api_key(legacy, b"aad") == "old-format"
    assert security.needs_reencryption(legacy)


def test_previous_key_decrypts_and_unknown_key_is_a_clear_error(rotate, monkeypatch) -> None:
    before = security.encrypt_api_key("value", b"x")
    legacy = _legacy_encrypt(settings.MASTER_ENCRYPTION_KEY, "legacy", b"x")
    old, new = rotate()
    assert security.decrypt_api_key(before, b"x") == "value"
    assert security.decrypt_api_key(legacy, b"x") == "legacy"
    assert security.needs_reencryption(before)
    after = security.reencrypt(before, b"x")
    assert after.startswith(f"v2:{security.current_key_id()}:") and security.decrypt_api_key(after, b"x") == "value"
    assert list(security.keyring()) == [security.key_id(base64.b64decode(new)), security.key_id(base64.b64decode(old))]
    monkeypatch.setattr(settings, "MASTER_ENCRYPTION_KEYS_PREVIOUS", "")
    with pytest.raises(security.KeyringError, match="not in MASTER_ENCRYPTION_KEY"):
        security.decrypt_api_key(before, b"x")
    with pytest.raises(InvalidTag):
        security.decrypt_api_key(legacy, b"x")


def test_malformed_previous_key_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(settings, "MASTER_ENCRYPTION_KEYS_PREVIOUS", "not-base64!!")
    with pytest.raises(RuntimeError, match="MASTER_ENCRYPTION_KEYS_PREVIOUS"):
        security.keyring()


def test_every_encrypted_column_is_registered_for_rotation() -> None:
    from app.models.base import Base

    registered = {(f.model.__tablename__, c) for f in key_rotation.ENCRYPTED_FIELDS for c in f.columns}
    found = {
        (table.name, col.name)
        for table in Base.metadata.tables.values()
        for col in table.columns
        if col.name.endswith("_enc") or col.name.startswith("encrypted_")
    }
    assert found and found <= registered


async def test_reencrypt_moves_accounts_and_totp_to_the_new_key(db, rotate) -> None:
    user, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    acc.encrypted_api_secret = _legacy_encrypt(settings.MASTER_ENCRYPTION_KEY, "legacy-secret", str(ws.id).encode())
    user.totp_secret_enc = mfa.encrypt_secret(user, "JBSWY3DPEHPK3PXP")
    await db.commit()

    rotate()
    status = (await key_rotation.run(db, reencrypt=False)).as_dict()
    assert status["pending"] == 3 and status["ciphertexts_by_key"].get("legacy") == 1

    dry = await key_rotation.run(db, reencrypt=True, dry_run=True)
    assert dry.reencrypted == 3
    assert (await key_rotation.run(db, reencrypt=False)).as_dict()["pending"] == 3  # nothing written

    report = await key_rotation.run(db, reencrypt=True, batch_size=1)
    assert report.ok and report.reencrypted == 3
    assert (await key_rotation.run(db, reencrypt=False)).as_dict()["pending"] == 0
    assert (await key_rotation.run(db, reencrypt=True)).reencrypted == 0  # idempotent

    acc = (await db.execute(select(Account).where(Account.id == acc.id))).scalar_one()
    user = (await db.execute(select(User).where(User.id == user.id))).scalar_one()
    settings.MASTER_ENCRYPTION_KEYS_PREVIOUS = ""  # old key gone: everything must still decrypt
    adapter = adapter_for_account(acc)
    assert adapter._api_key == "dummy-key-0000" and adapter._api_secret == b"legacy-secret"
    assert mfa.decrypt_secret(user, user.totp_secret_enc) == "JBSWY3DPEHPK3PXP"


async def test_reencrypt_reports_undecryptable_values_without_writing(db, monkeypatch) -> None:
    _, ws = await make_user_workspace(db)
    acc = await make_account(db, ws)
    acc.encrypted_api_key = _legacy_encrypt(_key(), "foreign", str(ws.id).encode())  # key not in the ring
    await db.commit()
    before = acc.encrypted_api_key
    report = await key_rotation.run(db, reencrypt=True)
    assert not report.ok and report.failed == [f"accounts.encrypted_api_key:{acc.id}: InvalidTag"]
    assert "foreign" not in str(report.as_dict())
    acc = (await db.execute(select(Account).where(Account.id == acc.id))).scalar_one()
    assert acc.encrypted_api_key == before


def test_audit_pin_keeps_the_chain_key(monkeypatch, capsys) -> None:
    from app import keys_cli

    monkeypatch.setattr(settings, "AUDIT_CHAIN_KEY", "")
    derived = audit_chain.chain_key()
    assert keys_cli.main(["audit-pin"]) == 0
    pinned = capsys.readouterr().out.strip().split("=", 1)[1]
    old = settings.MASTER_ENCRYPTION_KEY
    monkeypatch.setattr(settings, "MASTER_ENCRYPTION_KEY", _key())
    monkeypatch.setattr(settings, "MASTER_ENCRYPTION_KEYS_PREVIOUS", old)
    monkeypatch.setattr(settings, "AUDIT_CHAIN_KEY", pinned)
    assert audit_chain.chain_key() == derived
    monkeypatch.setattr(settings, "AUDIT_CHAIN_KEY", "")
    assert keys_cli.main(["audit-pin", "--previous"]) == 0
    assert capsys.readouterr().out.strip().split("=", 1)[1] == pinned


def test_generate_prints_a_valid_key(capsys) -> None:
    from app import keys_cli

    assert keys_cli.main(["generate"]) == 0
    assert len(base64.b64decode(capsys.readouterr().out.strip())) == 32


def test_production_requires_audit_pin_during_rotation_and_redis_password() -> None:
    base = dict(
        ENVIRONMENT="production",
        JWT_SECRET="x" * 48,
        MASTER_ENCRYPTION_KEY=_key(),
        DATABASE_URL="postgresql+asyncpg://u:pw@h/db",
        COOKIE_SECURE=True,
        DEBUG=False,
        REDIS_PASSWORD="pw",
        ALLOW_REGISTRATION=False,
    )
    assert Settings(**base).production_problems() == []
    problems = Settings(**{**base, "MASTER_ENCRYPTION_KEYS_PREVIOUS": _key()}).production_problems()
    assert any("AUDIT_CHAIN_KEY" in p for p in problems)
    assert (
        Settings(
            **{**base, "MASTER_ENCRYPTION_KEYS_PREVIOUS": _key(), "AUDIT_CHAIN_KEY": "b64:AA=="}
        ).production_problems()
        == []
    )
    assert any("Redis" in p for p in Settings(**{**base, "REDIS_PASSWORD": ""}).production_problems())
    assert any(
        "PREVIOUS" in p for p in Settings(**{**base, "MASTER_ENCRYPTION_KEYS_PREVIOUS": "abc"}).production_problems()
    )


def test_redis_password_is_injected_into_all_redis_urls() -> None:
    s = Settings(REDIS_PASSWORD="p@ss:w/rd", REDIS_USERNAME="isctrade")
    for url in (s.REDIS_URL, s.CELERY_BROKER_URL, s.CELERY_RESULT_BACKEND):
        assert url.startswith("redis://isctrade:p%40ss%3Aw%2Frd@")
    assert with_redis_auth("redis://redis:6379/0", "", "pw") == "redis://:pw@redis:6379/0"
    assert with_redis_auth("redis://u:x@redis:6379/0", "", "pw") == "redis://u:x@redis:6379/0"  # explicit wins
    assert with_redis_auth("memory://", "", "pw") == "memory://"
    assert "@" not in Settings(REDIS_PASSWORD="").REDIS_URL


def test_redis_url_with_password_parses_for_redis_py() -> None:
    from redis import Redis

    s = Settings(REDIS_PASSWORD="p@ss:w/rd", REDIS_USERNAME="isctrade")
    kw = Redis.from_url(s.REDIS_URL).connection_pool.connection_kwargs
    assert kw["username"] == "isctrade" and kw["password"] == "p@ss:w/rd"


def test_celery_uses_the_redis_urls_with_credentials() -> None:
    """Celery prefers the CELERY_* env vars over constructor args: celery_app aligns them with the settings."""
    from app.tasks.celery_app import celery_app

    assert os.environ["CELERY_BROKER_URL"] == settings.CELERY_BROKER_URL == celery_app.conf.broker_url
    assert os.environ["CELERY_RESULT_BACKEND"] == settings.CELERY_RESULT_BACKEND == celery_app.conf.result_backend
