"""Regression tests for the security audit (docs/AUDIT.md)."""

from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limiting import client_ip

PW = "Senha-Forte-123"


def _request(peer: str, xff: str | None = None) -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234), "method": "GET", "path": "/"})


def test_client_ip_ignores_forwarded_for_from_untrusted_peer() -> None:
    assert client_ip(_request("203.0.113.9", "1.2.3.4")) == "203.0.113.9"


def test_client_ip_uses_rightmost_untrusted_hop_behind_trusted_proxy() -> None:
    # attacker sends "X-Forwarded-For: 1.2.3.4"; Caddy (172.18.0.5) appends the real client 198.51.100.7
    assert client_ip(_request("172.18.0.5", "1.2.3.4, 198.51.100.7")) == "198.51.100.7"
    assert client_ip(_request("127.0.0.1", "198.51.100.7")) == "198.51.100.7"
    assert client_ip(_request("127.0.0.1")) == "127.0.0.1"


async def test_login_locks_email_after_repeated_failures(client, monkeypatch) -> None:
    monkeypatch.setattr(settings, "LOGIN_FAILURES_PER_EMAIL", 3)
    monkeypatch.setattr(settings, "LOGIN_RATE_LIMIT_PER_MINUTE", 100)
    r = await client.post("/api/v1/auth/register", json={"email": "victim@example.com", "password": PW})
    assert r.status_code == 201
    for i in range(3):
        # different (spoofed) source addresses do not help: the budget is per e-mail
        r = await client.post(
            "/api/v1/auth/login",
            json={"email": "victim@example.com", "password": "wrong-password"},
            headers={"X-Forwarded-For": f"10.9.9.{i}"},
        )
        assert r.status_code == 401
    r = await client.post("/api/v1/auth/login", json={"email": "victim@example.com", "password": PW})
    assert r.status_code == 429
    # unknown e-mails get the same 401 (no account enumeration)
    r = await client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": PW})
    assert r.status_code == 401


def test_news_urls_keep_only_http_links() -> None:
    from app.services.news.sources import RawNews, safe_url

    assert safe_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    for bad in ("javascript:alert(1)", "JaVaScRiPt:alert(1)", "data:text/html,x", "//evil.example", "ftp://x/y", ""):
        assert safe_url(bad) is None
    item = RawNews(source="rss", source_name="x", title="t", published_at=None, url=" javascript:alert(1)")  # type: ignore[arg-type]
    assert item.url is None


def test_redaction_masks_query_tokens() -> None:
    from app.core.redaction import redact_text

    text = redact_text("GET https://cryptopanic.com/api/?auth_token=abc123&public=true refresh_token=xyz")
    assert "abc123" not in text and "xyz" not in text
