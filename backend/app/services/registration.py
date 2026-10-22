"""User creation shared by signup and the operator CLI, plus single-use registration invites (G-03b/G-17).

- ``closed``: no self-service signup; the operator bootstraps the super-admin with ``python -m app.cli create-admin``.
- ``invite``: signup requires a valid, unused, unexpired invite (only its SHA-256 is stored; bound to an e-mail
  when one was given).
- ``open``: anyone may sign up (refused in production without ALLOW_OPEN_REGISTRATION_IN_PRODUCTION).

Outside production the very first signup still becomes super-admin (developer convenience); in production
signup never grants the platform role, and with zero users it is refused so that nobody can race the owner.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import Role, hash_password_async
from app.models import User, Workspace
from app.models.base import utcnow
from app.models.invite import RegistrationInvite


class RegistrationError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


def hash_invite_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def _aware(dt):
    from datetime import UTC

    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


async def create_invite(
    db: AsyncSession,
    *,
    created_by: uuid.UUID | None = None,
    email: str | None = None,
    note: str | None = None,
    hours: int | None = None,
) -> tuple[RegistrationInvite, str]:
    """Returns the invite and the clear token (shown once, never stored)."""
    token = secrets.token_urlsafe(32)
    invite = RegistrationInvite(
        token_hash=hash_invite_token(token),
        email=email.lower() if email else None,
        note=note,
        created_by_id=created_by,
        expires_at=utcnow() + timedelta(hours=hours or settings.INVITE_EXPIRE_HOURS),
    )
    db.add(invite)
    await db.flush()
    return invite, token


def invite_status(invite: RegistrationInvite) -> str:
    if invite.revoked_at:
        return "revoked"
    if invite.used_at:
        return "used"
    if _aware(invite.expires_at) <= utcnow():
        return "expired"
    return "pending"


async def _claim_invite(db: AsyncSession, token: str | None, email: str) -> RegistrationInvite:
    if not token:
        raise RegistrationError(403, "Cadastro somente por convite")
    invite = await db.scalar(
        select(RegistrationInvite).where(RegistrationInvite.token_hash == hash_invite_token(token)).with_for_update()
    )
    if invite is None or invite_status(invite) != "pending" or (invite.email and invite.email != email):
        raise RegistrationError(403, "Convite inválido, expirado ou já utilizado")
    return invite


async def create_user(
    db: AsyncSession, *, email: str, password: str, full_name: str | None = None, role: Role = Role.TRADER
) -> User:
    """User + personal workspace (flushed, not committed). Caller checks e-mail uniqueness."""
    user = User(
        email=email.lower(), full_name=full_name, hashed_password=await hash_password_async(password), role=role
    )
    db.add(user)
    await db.flush()
    workspace = Workspace(name="Workspace pessoal", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    user.active_workspace_id = workspace.id
    return user


async def register_user(
    db: AsyncSession, *, email: str, password: str, full_name: str | None, invite_token: str | None
) -> tuple[User, RegistrationInvite | None]:
    """Self-service signup under the configured REGISTRATION_MODE. Raises RegistrationError."""
    mode = settings.registration_mode
    if mode == "closed":
        raise RegistrationError(403, "Cadastro desabilitado")
    email = email.lower()
    has_users = (await db.scalar(select(func.count()).select_from(User))) > 0
    if settings.is_production and not has_users:
        raise RegistrationError(403, "Nenhum administrador configurado: use `python -m app.cli create-admin`")
    invite = await _claim_invite(db, invite_token, email) if mode == "invite" else None
    if await db.scalar(select(User).where(User.email == email)):
        raise RegistrationError(409, "E-mail já cadastrado")
    role = Role.ADMIN if not has_users and not settings.is_production else Role.TRADER
    user = await create_user(db, email=email, password=password, full_name=full_name, role=role)
    if invite is not None:
        invite.used_at = utcnow()
        invite.used_by_id = user.id
    return user, invite
