import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Pagination, pagination, require_superadmin
from app.api.v1.endpoints.history import paginate
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
from app.schemas.common import Page
from app.schemas.history import AuditRead
from app.schemas.user import AdminUserUpdate, UserRead
from app.services.audit import log_event
from app.services.kill_switch import get_global_kill_switch, set_global_kill_switch

router = APIRouter()


class KillSwitchIn(BaseModel):
    active: bool
    reason: str | None = Field(None, max_length=255)


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
