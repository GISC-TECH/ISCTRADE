import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Pagination, pagination, require_superadmin
from app.api.v1.endpoints.history import paginate
from app.core.config import settings
from app.core.database import get_db
from app.core.security import Role
from app.models import (
    Account,
    AccountStatus,
    AuditLog,
    BotConfig,
    BotState,
    Order,
    Trade,
    User,
    Workspace,
)
from app.models.base import utcnow
from app.models.invite import RegistrationInvite
from app.schemas.common import Page
from app.schemas.history import AuditRead
from app.schemas.user import AdminUserUpdate, UserRead
from app.services import audit_chain
from app.services.audit import log_event
from app.services.kill_switch import get_global_kill_switch, set_global_kill_switch
from app.services.mfa import clear_mfa, get_security_policy, set_security_policy
from app.services.registration import create_invite, invite_status

router = APIRouter()


class KillSwitchIn(BaseModel):
    active: bool
    reason: str | None = Field(None, max_length=255)


class SecurityPolicy(BaseModel):
    require_2fa_all: bool


class SecurityPolicyOut(SecurityPolicy):
    totp_required_for_live: bool
    totp_required_for_admins: bool
    live_trading_enabled: bool


class AdminWorkspace(BaseModel):
    id: uuid.UUID
    name: str
    owner_email: str
    accounts: int
    running_accounts: int


@router.get("/stats")
async def admin_stats(admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)) -> dict:
    async def count(model, *where) -> int:
        return int(await db.scalar(select(func.count()).select_from(model).where(*where)) or 0)

    return {
        "users": await count(User),
        "workspaces": await count(Workspace),
        "accounts": await count(Account),
        "accounts_running": await count(Account, Account.status == AccountStatus.RUNNING),
        "accounts_killed": await count(Account, Account.status == AccountStatus.KILLED),
        "bots": await count(BotConfig),
        "bots_running": await count(BotConfig, BotConfig.state == BotState.RUNNING),
        "bots_error": await count(BotConfig, BotConfig.state == BotState.ERROR),
        "orders": await count(Order),
        "trades": await count(Trade),
        "global_kill_switch": await get_global_kill_switch(db),
    }


@router.get("/users", response_model=Page[UserRead])
async def list_users(
    q: str | None = None,
    page: Pagination = Depends(pagination),
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(User)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(like), func.lower(User.full_name).like(like)))
    return await paginate(db, stmt, page, User.created_at)


@router.patch("/users/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID,
    data: AdminUserUpdate,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")
    changes = data.model_dump(exclude_unset=True)
    if user.id == admin.id and (changes.get("role", Role.ADMIN) != Role.ADMIN or changes.get("is_active") is False):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Você não pode remover seu próprio acesso de admin")
    for key, value in changes.items():
        setattr(user, key, value)
    if changes.get("is_active") is False:
        user.token_version += 1  # kick out active sessions
    await log_event(db, "admin.user_updated", level="warning", user_id=admin.id, target=user.email, changes=changes)
    await db.commit()
    await db.refresh(user)
    return user


@router.get("/workspaces", response_model=Page[AdminWorkspace])
async def list_all_workspaces(
    page: Pagination = Depends(pagination),
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    total = await db.scalar(select(func.count()).select_from(Workspace))
    accounts = (
        select(
            Account.workspace_id,
            func.count().label("n"),
            func.sum(case((Account.status == AccountStatus.RUNNING, 1), else_=0)).label("running"),
        )
        .group_by(Account.workspace_id)
        .subquery()
    )
    rows = await db.execute(
        select(Workspace, User.email, accounts.c.n, accounts.c.running)
        .join(User, User.id == Workspace.owner_id)
        .outerjoin(accounts, accounts.c.workspace_id == Workspace.id)
        .order_by(Workspace.created_at)
        .limit(page.limit)
        .offset(page.offset)
    )
    items = [
        AdminWorkspace(id=ws.id, name=ws.name, owner_email=email, accounts=n or 0, running_accounts=running or 0)
        for ws, email, n, running in rows
    ]
    return {"items": items, "total": total or 0, "limit": page.limit, "offset": page.offset}


@router.get("/kill-switch")
async def get_kill_switch(admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)) -> dict:
    return await get_global_kill_switch(db)


@router.post("/kill-switch")
async def toggle_kill_switch(
    data: KillSwitchIn, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
) -> dict:
    """Kill switch global: para todos os bots/contas e bloqueia novas ordens de abertura."""
    value = await set_global_kill_switch(db, data.active, admin.id, data.reason)
    await db.commit()
    return value


@router.get("/audit", response_model=Page[AuditRead])
async def global_audit(
    event: str | None = None,
    level: str | None = None,
    page: Pagination = Depends(pagination),
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AuditLog)
    if event:
        stmt = stmt.where(AuditLog.event.startswith(event))
    if level:
        stmt = stmt.where(AuditLog.level == level)
    return await paginate(db, stmt, page, AuditLog.created_at.desc())


@router.get("/audit/verify")
async def verify_audit_chain(
    seal: bool = False,
    anchor_seq: int | None = None,
    anchor_hash: str | None = None,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Verifica a trilha de auditoria encadeada (HMAC). `seal=true` sela antes as linhas pendentes.

    `anchor_seq` + `anchor_hash` conferem contra uma âncora externa (hash da cabeça guardado fora do banco).
    """
    if (anchor_seq is None) != (anchor_hash is None):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "anchor_seq e anchor_hash devem ser informados juntos")
    if seal:
        await audit_chain.seal_pending(db)
    anchor = (anchor_seq, anchor_hash) if anchor_seq is not None and anchor_hash is not None else None
    report = await audit_chain.verify_chain(db, expected_head=anchor)
    return report.as_dict()


def _policy_out(policy: dict) -> SecurityPolicyOut:
    return SecurityPolicyOut(
        require_2fa_all=policy["require_2fa_all"],
        totp_required_for_live=settings.TOTP_REQUIRED_FOR_LIVE,
        totp_required_for_admins=settings.TOTP_REQUIRED_FOR_ADMINS,
        live_trading_enabled=settings.LIVE_TRADING_ENABLED,
    )


@router.get("/security-policy", response_model=SecurityPolicyOut)
async def get_policy(admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)):
    return _policy_out(await get_security_policy(db))


@router.put("/security-policy", response_model=SecurityPolicyOut)
async def update_policy(
    data: SecurityPolicy, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    """Exigir 2FA de todos os usuários (quem não tiver ativado fica restrito à página Segurança)."""
    policy = await set_security_policy(db, data.require_2fa_all)
    await log_event(db, "admin.security_policy_updated", level="warning", user_id=admin.id, **policy)
    await db.commit()
    return _policy_out(policy)


@router.post("/users/{user_id}/reset-2fa", response_model=UserRead)
async def reset_user_2fa(
    user_id: uuid.UUID, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
):
    """Remove o 2FA de um usuário que perdeu o autenticador e os códigos (encerra as sessões dele)."""
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado")
    if user.id == admin.id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Use a página Segurança para alterar o seu próprio 2FA")
    clear_mfa(user)
    user.token_version += 1
    await log_event(db, "admin.user_2fa_reset", level="critical", user_id=admin.id, target=user.email)
    await db.commit()
    await db.refresh(user)
    return user


# ------------------------------------------------------------------ registration invites (G-03b)
class InviteIn(BaseModel):
    email: EmailStr | None = None
    note: str | None = Field(None, max_length=255)
    hours: int | None = Field(None, ge=1, le=24 * 90)


class InviteRead(BaseModel):
    id: uuid.UUID
    email: str | None
    note: str | None
    status: str
    created_at: datetime
    expires_at: datetime
    used_at: datetime | None = None
    used_by_id: uuid.UUID | None = None


class InviteCreated(InviteRead):
    token: str  # shown once; only its SHA-256 is stored
    registration_mode: str


def _invite_read(inv: RegistrationInvite) -> dict:
    return {
        "id": inv.id, "email": inv.email, "note": inv.note, "status": invite_status(inv),
        "created_at": inv.created_at, "expires_at": inv.expires_at, "used_at": inv.used_at,
        "used_by_id": inv.used_by_id,
    }  # fmt: skip


@router.post("/invites", response_model=InviteCreated, status_code=status.HTTP_201_CREATED)
async def create_registration_invite(
    data: InviteIn, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
) -> dict:
    invite, token = await create_invite(db, created_by=admin.id, email=data.email, note=data.note, hours=data.hours)
    await log_event(db, "admin.invite_created", user_id=admin.id, invite_id=str(invite.id), email=invite.email)
    await db.commit()
    await db.refresh(invite)
    return {**_invite_read(invite), "token": token, "registration_mode": settings.registration_mode}


@router.get("/invites", response_model=list[InviteRead])
async def list_registration_invites(
    admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    rows = await db.scalars(select(RegistrationInvite).order_by(RegistrationInvite.created_at.desc()).limit(200))
    return [_invite_read(inv) for inv in rows]


@router.delete("/invites/{invite_id}", response_model=InviteRead)
async def revoke_registration_invite(
    invite_id: uuid.UUID, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
) -> dict:
    invite = await db.get(RegistrationInvite, invite_id)
    if invite is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Convite não encontrado")
    if invite_status(invite) == "pending":
        invite.revoked_at = utcnow()
        await log_event(db, "admin.invite_revoked", user_id=admin.id, invite_id=str(invite.id))
        await db.commit()
        await db.refresh(invite)
    return _invite_read(invite)
