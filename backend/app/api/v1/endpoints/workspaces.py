import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_current_user, resolve_workspace
from app.core.database import get_db
from app.core.security import Role
from app.models import Account, AccountStatus, User, Workspace, WorkspaceMember
from app.schemas.workspace import (
    MemberAdd,
    MemberRead,
    MemberUpdate,
    WorkspaceCreate,
    WorkspaceRead,
    WorkspaceUpdate,
)
from app.services.audit import log_event

router = APIRouter()


async def _ctx(workspace_id: uuid.UUID, user: User, db: AsyncSession, role: Role = Role.VIEWER) -> WorkspaceContext:
    ctx = await resolve_workspace(db, user, workspace_id)
    ctx.require(role)
    return ctx


def _read(ws: Workspace, role: Role | None, user: User) -> WorkspaceRead:
    out = WorkspaceRead.model_validate(ws)
    out.role = role
    out.is_active = user.active_workspace_id == ws.id
    return out


@router.get("/", response_model=list[WorkspaceRead])
async def list_workspaces(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(Workspace, WorkspaceMember.role)
        .outerjoin(
            WorkspaceMember, (WorkspaceMember.workspace_id == Workspace.id) & (WorkspaceMember.user_id == user.id)
        )
        .where(or_(Workspace.owner_id == user.id, WorkspaceMember.user_id == user.id))
        .order_by(Workspace.created_at)
    )
    return [_read(ws, Role.ADMIN if ws.owner_id == user.id else role, user) for ws, role in rows]


@router.post("/", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    data: WorkspaceCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    ws = Workspace(name=data.name, owner_id=user.id)
    db.add(ws)
    await db.flush()
    await log_event(db, "workspace.created", user_id=user.id, workspace_id=ws.id, name=data.name)
    await db.commit()
    await db.refresh(ws)
    return _read(ws, Role.ADMIN, user)


@router.get("/{workspace_id}", response_model=WorkspaceRead)
async def get_workspace(workspace_id: uuid.UUID, user: User = Depends(get_current_user), db=Depends(get_db)):
    ctx = await _ctx(workspace_id, user, db)
    return _read(ctx.workspace, ctx.role, user)


@router.patch("/{workspace_id}", response_model=WorkspaceRead)
async def update_workspace(
    workspace_id: uuid.UUID, data: WorkspaceUpdate, user: User = Depends(get_current_user), db=Depends(get_db)
):
    ctx = await _ctx(workspace_id, user, db, Role.ADMIN)
    ctx.workspace.name = data.name
    await log_event(db, "workspace.updated", user_id=user.id, workspace_id=workspace_id, name=data.name)
    await db.commit()
    await db.refresh(ctx.workspace)
    return _read(ctx.workspace, ctx.role, user)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(workspace_id: uuid.UUID, user: User = Depends(get_current_user), db=Depends(get_db)):
    ctx = await _ctx(workspace_id, user, db, Role.ADMIN)
    if ctx.workspace.owner_id != user.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Somente o dono pode excluir o workspace")
    owned = await db.scalar(select(func.count()).select_from(Workspace).where(Workspace.owner_id == user.id))
    if owned <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Você precisa manter ao menos um workspace")
    running = await db.scalar(
        select(func.count())
        .select_from(Account)
        .where(Account.workspace_id == workspace_id, Account.status == AccountStatus.RUNNING)
    )
    if running:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Pare todas as contas antes de excluir")
    for u in await db.scalars(select(User).where(User.active_workspace_id == workspace_id)):
        u.active_workspace_id = None
    await log_event(db, "workspace.deleted", user_id=user.id, name=ctx.workspace.name)
    await db.delete(ctx.workspace)
    await db.commit()


@router.post("/{workspace_id}/activate", response_model=WorkspaceRead)
async def activate_workspace(workspace_id: uuid.UUID, user: User = Depends(get_current_user), db=Depends(get_db)):
    """Define o workspace ativo do usuário (usado quando o header X-Workspace-ID não é enviado)."""
    ctx = await _ctx(workspace_id, user, db)
    user.active_workspace_id = ctx.workspace.id
    await db.commit()
    return _read(ctx.workspace, ctx.role, user)


@router.get("/{workspace_id}/members", response_model=list[MemberRead])
async def list_members(workspace_id: uuid.UUID, user: User = Depends(get_current_user), db=Depends(get_db)):
    ctx = await _ctx(workspace_id, user, db)
    owner = await db.get(User, ctx.workspace.owner_id)
    out = [MemberRead(user_id=owner.id, email=owner.email, full_name=owner.full_name, role=Role.ADMIN, is_owner=True)]
    rows = await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at)
    )
    out += [MemberRead(user_id=u.id, email=u.email, full_name=u.full_name, role=m.role) for m, u in rows]
    return out


@router.post("/{workspace_id}/members", response_model=MemberRead, status_code=status.HTTP_201_CREATED)
async def add_member(
    workspace_id: uuid.UUID, data: MemberAdd, user: User = Depends(get_current_user), db=Depends(get_db)
):
    ctx = await _ctx(workspace_id, user, db, Role.ADMIN)
    target = await db.scalar(select(User).where(User.email == data.email.lower()))
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Usuário não encontrado (ele precisa se cadastrar)")
    if target.id == ctx.workspace.owner_id:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="O dono já faz parte do workspace")
    exists = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == target.id
        )
    )
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Usuário já é membro")
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=target.id, role=data.role))
    await log_event(
        db, "workspace.member_added", user_id=user.id, workspace_id=workspace_id, member=target.email, role=data.role
    )
    await db.commit()
    return MemberRead(user_id=target.id, email=target.email, full_name=target.full_name, role=data.role)


async def _member(db: AsyncSession, workspace_id: uuid.UUID, member_id: uuid.UUID) -> WorkspaceMember:
    m = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == member_id
        )
    )
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Membro não encontrado")
    return m


@router.patch("/{workspace_id}/members/{member_id}", response_model=MemberRead)
async def update_member(
    workspace_id: uuid.UUID,
    member_id: uuid.UUID,
    data: MemberUpdate,
    user: User = Depends(get_current_user),
    db=Depends(get_db),
):
    await _ctx(workspace_id, user, db, Role.ADMIN)
    m = await _member(db, workspace_id, member_id)
    m.role = data.role
    target = await db.get(User, member_id)
    await log_event(db, "workspace.member_updated", user_id=user.id, workspace_id=workspace_id, member=target.email,
                    role=data.role)  # fmt: skip
    await db.commit()
    return MemberRead(user_id=target.id, email=target.email, full_name=target.full_name, role=m.role)


@router.delete("/{workspace_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    workspace_id: uuid.UUID, member_id: uuid.UUID, user: User = Depends(get_current_user), db=Depends(get_db)
):
    ctx = await _ctx(workspace_id, user, db)
    if member_id != user.id:  # leaving is always allowed; removing others needs admin
        ctx.require(Role.ADMIN)
    m = await _member(db, workspace_id, member_id)
    target = await db.get(User, member_id)
    if target and target.active_workspace_id == workspace_id:
        target.active_workspace_id = None
    await db.delete(m)
    await log_event(db, "workspace.member_removed", user_id=user.id, workspace_id=workspace_id,
                    member=target.email if target else str(member_id))  # fmt: skip
    await db.commit()
