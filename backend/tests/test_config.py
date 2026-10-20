from app.core.config import Settings


def test_cors_origins_accepts_csv_and_json(monkeypatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://a.com, http://b.com")
    assert Settings().CORS_ORIGINS == ["http://a.com", "http://b.com"]
    monkeypatch.setenv("CORS_ORIGINS", '["http://c.com"]')
    assert Settings().CORS_ORIGINS == ["http://c.com"]
