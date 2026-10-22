"""TOTP 2FA: RFC 6238 vectors, enrolment, login step-up, recovery codes, policy, live gate and disclaimer."""

import uuid

import pytest
from sqlalchemy import select

from app.core import totp
from app.core.config import settings
from app.core.legal import DISCLAIMER_VERSION
from app.models import Account, ExecutionMode, User, Workspace
from app.services import mfa
from tests.factories import make_account
from tests.test_api_e2e import PW, bearer, enable_totp, register_login

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890"), RFC 6238 appendix B


def test_rfc6238_vectors_and_replay_window() -> None:
    # RFC 6238 SHA-1 vectors truncated to 6 digits: T=59 -> 94287082, T=1111111109 -> 07081804
    assert totp.code_at(RFC_SECRET, 59 // 30) == "287082"
    assert totp.code_at(RFC_SECRET, 1111111109 // 30) == "081804"
    now = 1111111109.0
    step = totp.current_step(now)
    assert totp.verify_totp(RFC_SECRET, "081804", now=now) == step
    assert totp.verify_totp(RFC_SECRET, "081 804", now=now + 30) == step  # previous step accepted (drift)
    assert totp.verify_totp(RFC_SECRET, "081804", now=now + 90) is None  # outside the window
    assert totp.verify_totp(RFC_SECRET, "081804", last_used_step=step, now=now) is None  # replay
    assert totp.verify_totp(RFC_SECRET, "abcdef", now=now) is None
    uri = totp.provisioning_uri(RFC_SECRET, "a@b.com", "ISCTRADE")
    assert uri.startswith("otpauth://totp/ISCTRADE:a@b.com?") and "secret=" in uri
    assert totp.qr_svg_data_uri(uri).startswith("data:image/svg+xml;base64,")


def test_recovery_codes_are_salted_hashes() -> None:
    codes = totp.generate_recovery_codes()
    assert len(codes) == 10 and len(set(codes)) == 10
    stored = [totp.hash_recovery_code(c) for c in codes]
    assert all(c not in s for c, s in zip(codes, stored, strict=True))
    assert totp.hash_recovery_code(codes[0]) != stored[0]  # random salt
    assert totp.recovery_code_matches(codes[0].lower().replace("-", ""), stored[0])
    assert not totp.recovery_code_matches(codes[1], stored[0])
    assert totp.looks_like_recovery_code(codes[0]) and not totp.looks_like_recovery_code("123456")


async def test_enrolment_encrypts_secret_and_hashes_codes(client, db) -> None:
    h = bearer(await register_login(client, "mfa@example.com"))
    st = (await client.get("/api/v1/auth/2fa", headers=h)).json()
    assert st["enabled"] is False and st["pending"] is False and st["live_requires_2fa"] is True
    setup = (await client.post("/api/v1/auth/2fa/setup", headers=h)).json()
    assert setup["qr_svg"].startswith("data:image/svg+xml") and setup["otpauth_uri"].startswith("otpauth://")
    assert (await client.get("/api/v1/auth/2fa", headers=h)).json()["pending"] is True
    bad = await client.post("/api/v1/auth/2fa/enable", json={"code": "000000"}, headers=h)
    assert bad.status_code == 400
    code = totp.code_at(setup["secret"], totp.current_step())
    if code == "000000":  # pragma: no cover - 1 in a million
        pytest.skip("unlucky code")
    r = await client.post("/api/v1/auth/2fa/enable", json={"code": code}, headers=h)
    assert r.status_code == 200 and len(r.json()["recovery_codes"]) == 10
    codes = r.json()["recovery_codes"]
    user = await db.scalar(select(User).where(User.email == "mfa@example.com"))
    await db.refresh(user)
    assert user.totp_enabled and user.totp_secret_enc and setup["secret"] not in user.totp_secret_enc
    assert mfa.decrypt_secret(user, user.totp_secret_enc) == setup["secret"]
    assert user.totp_pending_secret_enc is None
    assert all(c not in str(user.totp_recovery_codes) for c in codes)
    # already enabled: setup again is refused; status reports it
    assert (await client.post("/api/v1/auth/2fa/setup", headers=h)).status_code == 409
    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert me["totp_enabled"] is True


async def _login(client, email: str):
    client.cookies.clear()
    return await client.post("/api/v1/auth/login", json={"email": email, "password": PW})


async def test_login_step_up_recovery_codes_and_rate_limit(client) -> None:
    h = bearer(await register_login(client, "step@example.com"))
    secret = await enable_totp(client, h)
    codes = (await client.post(
        "/api/v1/auth/2fa/recovery-codes",
        json={"password": PW, "code": totp.code_at(secret, totp.current_step() + 1)}, headers=h,
    )).json()["recovery_codes"]  # fmt: skip

    r = await _login(client, "step@example.com")
    assert r.status_code == 200 and r.json()["mfa_required"] is True and "access_token" not in r.json()
    assert "access_token" not in client.cookies  # no session before the second factor
    token = r.json()["mfa_token"]
    r = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": "x" * 20, "code": "123456"})
    assert r.status_code == 401
    # code already used (replay of the step consumed above) is refused
    used = totp.code_at(secret, totp.current_step() + 1)
    assert (await client.post("/api/v1/auth/login/2fa", json={"mfa_token": token, "code": used})).status_code == 400
    # recovery code works once
    r = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": token, "code": codes[0]})
    assert r.status_code == 200 and r.json()["user"]["email"] == "step@example.com"
    assert "access_token" in client.cookies
    r = await _login(client, "step@example.com")
    again = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r.json()["mfa_token"], "code": codes[0]})
    assert again.status_code == 400
    # per-user failure budget: TOTP_MAX_FAILURES wrong codes (incl. the 2 above) -> 429, even with a valid code
    for _ in range(settings.TOTP_MAX_FAILURES - 2):
        await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r.json()["mfa_token"], "code": "999999"})
    blocked = await client.post("/api/v1/auth/login/2fa", json={"mfa_token": r.json()["mfa_token"], "code": codes[1]})
    assert blocked.status_code == 429


async def test_disable_requires_password_and_code(client, db) -> None:
    await register_login(client, "first@example.com")  # first user = super-admin (2FA mandatory for /admin)
    h = bearer(await register_login(client, "dis@example.com"))
    secret = await enable_totp(client, h)
    nxt = totp.code_at(secret, totp.current_step() + 1)
    r = await client.post("/api/v1/auth/2fa/disable", json={"password": "wrong-pass", "code": nxt}, headers=h)
    assert r.status_code == 400 and r.json()["detail"] == "Senha incorreta"
    r = await client.post("/api/v1/auth/2fa/disable", json={"password": PW, "code": "000001"}, headers=h)
    assert r.status_code == 400
    r = await client.post("/api/v1/auth/2fa/disable", json={"password": PW, "code": nxt}, headers=h)
    assert r.status_code == 200 and r.json()["enabled"] is False
    user = await db.scalar(select(User).where(User.email == "dis@example.com"))
    await db.refresh(user)
    assert user.totp_secret_enc is None and user.totp_recovery_codes == []
    # super-admin cannot disable a mandatory 2FA
    admin = bearer((await _login(client, "first@example.com")).json())
    s2 = await enable_totp(client, admin)
    r = await client.post(
        "/api/v1/auth/2fa/disable",
        json={"password": PW, "code": totp.code_at(s2, totp.current_step() + 1)},
        headers=admin,
    )
    assert r.status_code == 409


async def test_admin_policy_require_all_and_reset(client) -> None:
    admin = bearer(await register_login(client, "root@example.com"))
    trader = bearer(await register_login(client, "t@example.com"))
    client.cookies.clear()
    r = await client.put("/api/v1/admin/security-policy", json={"require_2fa_all": True}, headers=admin)
    assert r.status_code == 403
    await enable_totp(client, admin)
    r = await client.put("/api/v1/admin/security-policy", json={"require_2fa_all": True}, headers=admin)
    assert r.status_code == 200 and r.json()["require_2fa_all"] is True
    assert (await client.get("/api/v1/admin/security-policy", headers=admin)).json()["require_2fa_all"] is True
    # trader without 2FA: API blocked with a machine-readable header, /auth/me still works
    r = await client.get("/api/v1/accounts/", headers=trader)
    assert r.status_code == 403 and r.headers["x-mfa-enrollment-required"] == "policy"
    me = (await client.get("/api/v1/auth/me", headers=trader)).json()
    assert me["mfa_required"] is True and me["mfa_reason"] == "policy" and me["totp_enabled"] is False
    await enable_totp(client, trader)
    assert (await client.get("/api/v1/accounts/", headers=trader)).status_code == 200
    # admin resets the trader's 2FA (lost device): sessions revoked
    tid = me["id"]
    r = await client.post(f"/api/v1/admin/users/{tid}/reset-2fa", headers=admin)
    assert r.status_code == 200 and r.json()["totp_enabled"] is False
    assert (await client.get("/api/v1/auth/me", headers=trader)).status_code == 401
    assert (await client.post(f"/api/v1/admin/users/{uuid.uuid4()}/reset-2fa", headers=admin)).status_code == 404
    r = await client.put("/api/v1/admin/security-policy", json={"require_2fa_all": False}, headers=admin)
    assert r.json()["require_2fa_all"] is False


async def test_live_trading_requires_2fa(client, db, monkeypatch) -> None:
    await register_login(client, "first@example.com")
    login = await register_login(client, "live@example.com")
    h = bearer(login)
    user = await db.get(User, uuid.UUID(login["user"]["id"]))
    ws = await db.get(Workspace, user.active_workspace_id)
    acc = await make_account(db, ws, label="real", execution_mode=ExecutionMode.LIVE)
    await db.commit()
    order = {"account_id": str(acc.id), "symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": "0.01",
             "stop_loss": "40000", "confirm": True}  # fmt: skip
    r = await client.post("/api/v1/trading/orders", json=order, headers=h)
    assert r.status_code == 403 and r.headers["x-mfa-enrollment-required"] == "live"
    r = await client.post("/api/v1/trading/leverage", json={"account_id": str(acc.id), "symbol": "BTCUSDT",
                                                            "leverage": 2, "confirm": True}, headers=h)  # fmt: skip
    assert r.status_code == 403
    # server with live trading enabled: everyone who can trade a live account must enrol before using the API
    monkeypatch.setattr(settings, "LIVE_TRADING_ENABLED", True)
    r = await client.get("/api/v1/bots/", headers=h)
    assert r.status_code == 403 and r.headers["x-mfa-enrollment-required"] == "live"
    assert await mfa.can_trade_live(db, user)
    assert (await client.get("/api/v1/auth/me", headers=h)).json()["mfa_reason"] == "live"
    await enable_totp(client, h)
    assert (await client.get("/api/v1/bots/", headers=h)).status_code == 200
    r = await client.post(f"/api/v1/accounts/{acc.id}/start", headers=h)
    assert r.status_code == 200
    # paper-only users are not affected
    other = await db.scalar(select(User).where(User.email == "first@example.com"))
    assert not await mfa.can_trade_live(db, other)
    assert await db.scalar(select(Account).where(Account.id == acc.id))


async def test_disclaimer_acknowledgement(client) -> None:
    h = bearer(await register_login(client, "d@example.com"))
    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert me["disclaimer_pending"] is True and me["disclaimer_version"] is None
    assert (await client.post("/api/v1/auth/me/disclaimer", json={"version": "old"}, headers=h)).status_code == 409
    r = await client.post("/api/v1/auth/me/disclaimer", json={"version": DISCLAIMER_VERSION}, headers=h)
    assert r.status_code == 200 and r.json()["disclaimer_pending"] is False
    assert r.json()["disclaimer_version"] == DISCLAIMER_VERSION
