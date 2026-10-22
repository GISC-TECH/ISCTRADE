from app.core.config import Settings


def test_cors_origins_accepts_csv_and_json(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://a.com, http://b.com")
    assert Settings().CORS_ORIGINS == ["http://a.com", "http://b.com"]
    monkeypatch.setenv("CORS_ORIGINS", '["http://c.com"]')
    assert Settings().CORS_ORIGINS == ["http://c.com"]


def test_production_problems_flags_insecure_settings():
    from app.core.config import Settings

    bad = Settings(
        ENVIRONMENT="production",
        JWT_SECRET="CHANGE_ME",
        MASTER_ENCRYPTION_KEY="CHANGE_ME",
        DATABASE_URL="postgresql+asyncpg://u:CHANGE_ME@h/db",
        COOKIE_SECURE=False,
        DEBUG=True,
        ALLOW_REGISTRATION=True,
    )
    problems = bad.production_problems()
    assert len(problems) == 7  # + registro aberto (G-03b) + Redis sem senha (G-27)
    assert not any("CHANGE_ME" == p for p in problems)


def test_production_problems_accepts_strong_settings():
    import base64
    import os
    import secrets

    from app.core.config import Settings

    good = Settings(
        ENVIRONMENT="production",
        JWT_SECRET=secrets.token_urlsafe(48),
        MASTER_ENCRYPTION_KEY=base64.b64encode(os.urandom(32)).decode(),
        DATABASE_URL="postgresql+asyncpg://u:pw@h/db",
        REDIS_PASSWORD=secrets.token_urlsafe(24),
        COOKIE_SECURE=True,
        DEBUG=False,
        ALLOW_REGISTRATION=False,
    )
    assert good.is_production
    assert good.production_problems() == []


def test_check_production_settings_blocks_startup(monkeypatch):
    import pytest

    from app import main

    monkeypatch.setattr(main.settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(main.settings, "COOKIE_SECURE", False)
    with pytest.raises(RuntimeError, match="COOKIE_SECURE"):
        main.check_production_settings()
