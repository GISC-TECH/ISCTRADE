"""TOTP 2FA policy and verification (enrolment, login step-up, live-trading gate).

Policy (who must have 2FA):
- every user, when a platform admin turned on ``require_2fa_all`` (``system_settings['security_policy']``);
- anyone who can trade live (role >= trader in a workspace that has a live account) while
  ``LIVE_TRADING_ENABLED`` and ``TOTP_REQUIRED_FOR_LIVE`` are on;
- platform super-admins for ``/admin`` (``TOTP_REQUIRED_FOR_ADMINS``).
Independently of the global policy, live actions themselves (switching/creating a live account, starting it,
manual live orders) always require 2FA while ``TOTP_REQUIRED_FOR_LIVE`` is on.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.config import settings
from app.core.rate_limiting import hit as rate_hit
from app.core.rate_limiting import peek as rate_peek
from app.core.security import Role, decrypt_api_key, encrypt_api_key
from app.models.account import Account
from app.models.base import utcnow
from app.models.enums import ExecutionMode
from app.models.system import SystemSetting
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

SECURITY_POLICY_KEY = "security_policy"
FAILURE_WINDOW = 15 * 60
MFA_ENROLLMENT_HEADER = "X-MFA-Enrollment-Required"
ENROLLMENT_REQUIRED_DETAIL = "Autenticação em dois fatores (2FA) obrigatória. Ative o 2FA em Segurança para continuar."
LIVE_REQUIRES_MFA_DETAIL = "Ative a autenticação em dois fatores (2FA) em Segurança para operar contas reais (live)."
ADMIN_REQUIRES_MFA_DETAIL = "Ative a autenticação em dois fatores (2FA) em Segurança para acessar a administração."


class MfaError(Exception):
    def __init__(self, status_code: int, detail: str, headers: dict[str, str] | None = None):
        self.status_code, self.detail, self.headers = status_code, detail, headers


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
async def get_security_policy(db: AsyncSession) -> dict[str, Any]:
    row = await db.get(SystemSetting, SECURITY_POLICY_KEY)
    value = dict(row.value) if row and row.value else {}
    return {"require_2fa_all": bool(value.get("require_2fa_all", False))}


async def set_security_policy(db: AsyncSession, require_2fa_all: bool) -> dict[str, Any]:
    row = await db.get(SystemSetting, SECURITY_POLICY_KEY)
    value = {"require_2fa_all": bool(require_2fa_all)}
    if row is None:
        db.add(SystemSetting(key=SECURITY_POLICY_KEY, value=value))
    else:
        row.value = value
    return value


async def can_trade_live(db: AsyncSession, user: User) -> bool:
    """User has role >= trader in a workspace that has at least one live account."""
    live = exists().where(Account.workspace_id == Workspace.id, Account.execution_mode == ExecutionMode.LIVE)
    member = exists().where(
        WorkspaceMember.workspace_id == Workspace.id,
        WorkspaceMember.user_id == user.id,
        WorkspaceMember.role.in_([Role.TRADER, Role.ADMIN]),
    )
    stmt = select(Workspace.id).where(live, or_(Workspace.owner_id == user.id, member)).limit(1)
    return (await db.scalar(stmt)) is not None


async def mfa_required_reason(db: AsyncSession, user: User) -> str | None:
    """Why this user must have 2FA (None = optional). Cheap when the user already has 2FA."""
    if (await get_security_policy(db))["require_2fa_all"]:
        return "policy"
    if settings.TOTP_REQUIRED_FOR_LIVE and settings.LIVE_TRADING_ENABLED and await can_trade_live(db, user):
        return "live"
    if settings.TOTP_REQUIRED_FOR_ADMINS and user.role == Role.ADMIN:
        return "admin"
    return None


async def enforce_enrollment(db: AsyncSession, user: User) -> None:
    """Block the API (403 + header) for users that must have 2FA and have not enrolled yet.

    The admin rule is enforced only on /admin (see ``require_admin_mfa``) so a fresh install is still usable."""
    if user.totp_enabled:
        return
    reason = await mfa_required_reason(db, user)
    if reason in ("policy", "live"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ENROLLMENT_REQUIRED_DETAIL,
            headers={MFA_ENROLLMENT_HEADER: reason},
        )


def require_live_mfa(user: User) -> None:
    if settings.TOTP_REQUIRED_FOR_LIVE and not user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=LIVE_REQUIRES_MFA_DETAIL,
            headers={MFA_ENROLLMENT_HEADER: "live"},
        )


def require_admin_mfa(user: User) -> None:
    if settings.TOTP_REQUIRED_FOR_ADMINS and not user.totp_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=ADMIN_REQUIRES_MFA_DETAIL,
            headers={MFA_ENROLLMENT_HEADER: "admin"},
        )


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #
def _aad(user: User) -> bytes:
    return f"totp:{user.id}".encode()


def encrypt_secret(user: User, secret: str) -> str:
    return encrypt_api_key(secret, _aad(user))


def decrypt_secret(user: User, token: str) -> str:
    return decrypt_api_key(token, _aad(user))


def start_enrollment(user: User) -> dict[str, str]:
    secret = totp.generate_secret()
    user.totp_pending_secret_enc = encrypt_secret(user, secret)
    uri = totp.provisioning_uri(secret, user.email, settings.TOTP_ISSUER)
    return {"secret": secret, "otpauth_uri": uri, "qr_svg": totp.qr_svg_data_uri(uri)}


def new_recovery_codes(user: User) -> list[str]:
    codes = totp.generate_recovery_codes()
    user.totp_recovery_codes = [totp.hash_recovery_code(c) for c in codes]
    return codes


def clear_mfa(user: User) -> None:
    user.totp_enabled = False
    user.totp_secret_enc = None
    user.totp_pending_secret_enc = None
    user.totp_enabled_at = None
    user.totp_last_step = None
    user.totp_recovery_codes = []


# --------------------------------------------------------------------------- #
# Verification with per-user failure budget
# --------------------------------------------------------------------------- #
def _failures_key(user: User) -> str:
    return f"rl:2fa-fail:{user.id}"


async def _check_budget(user: User) -> None:
    if await rate_peek(_failures_key(user), FAILURE_WINDOW) >= settings.TOTP_MAX_FAILURES:
        raise MfaError(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Muitas tentativas de código 2FA. Aguarde alguns minutos e tente novamente.",
            {"Retry-After": str(FAILURE_WINDOW)},
        )


async def _fail(user: User) -> None:
    await rate_hit(_failures_key(user), FAILURE_WINDOW)
    raise MfaError(status.HTTP_400_BAD_REQUEST, "Código 2FA inválido")


def _accept_step(user: User, secret: str, code: str) -> bool:
    step = totp.verify_totp(secret, code, user.totp_last_step)
    if step is None:
        return False
    user.totp_last_step = step
    return True


async def verify_pending(user: User, code: str) -> None:
    """Confirm enrolment with a code from the new (pending) secret."""
    await _check_budget(user)
    if not user.totp_pending_secret_enc:
        raise MfaError(status.HTTP_409_CONFLICT, "Nenhuma configuração de 2FA em andamento")
    secret = decrypt_secret(user, user.totp_pending_secret_enc)
    step = totp.verify_totp(secret, code)
    if step is None:
        await _fail(user)
    user.totp_secret_enc, user.totp_pending_secret_enc = user.totp_pending_secret_enc, None
    user.totp_last_step = step
    user.totp_enabled, user.totp_enabled_at = True, utcnow()


async def verify_user_code(user: User, code: str, allow_recovery: bool = True) -> str:
    """Check a TOTP (or, when allowed, a single-use recovery code). Returns "totp" or "recovery"."""
    await _check_budget(user)
    if not user.totp_enabled or not user.totp_secret_enc:
        raise MfaError(status.HTTP_409_CONFLICT, "2FA não está ativado")
    if _accept_step(user, decrypt_secret(user, user.totp_secret_enc), code):
        return "totp"
    if allow_recovery and totp.looks_like_recovery_code(code):
        remaining = list(user.totp_recovery_codes or [])
        for stored in remaining:
            if totp.recovery_code_matches(code, stored):
                remaining.remove(stored)
                user.totp_recovery_codes = remaining  # new list -> JSON column change is detected
                return "recovery"
    await _fail(user)
    return ""  # pragma: no cover - _fail always raises


def mfa_status(user: User) -> dict[str, Any]:
    enabled_at: datetime | None = user.totp_enabled_at
    return {
        "enabled": bool(user.totp_enabled),
        "pending": bool(user.totp_pending_secret_enc),
        "enabled_at": enabled_at,
        "recovery_codes_remaining": len(user.totp_recovery_codes or []),
    }
