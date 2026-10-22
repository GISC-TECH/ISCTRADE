"""Shared API dependencies: current user, active workspace (tenant isolation) and permissions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Cookie, Depends, Header, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import Role, TokenError, has_role, verify_token
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember
from app.services.mfa import enforce_enrollment, require_admin_mfa

ACCESS_COOKIE_NAME = "access_token"
_bearer = HTTPBearer(auto_error=False)


async def get_current_user_base(
    db: AsyncSession = Depends(get_db),
    access_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    token = (credentials.credentials if credentials else None) or access_token
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")
    try:
        payload = verify_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido ou expirado") from None
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inválido")
    if payload.get("ver", 0) != user.token_version:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão revogada")
    return user


async def get_current_user(user: User = Depends(get_current_user_base), db: AsyncSession = Depends(get_db)) -> User:
    """Authenticated user, blocked (403 + X-MFA-Enrollment-Required) when 2FA is mandatory and not enrolled.

    Only the auth/2FA enrolment endpoints use ``get_current_user_base`` directly."""
    await enforce_enrollment(db, user)
    return user


async def require_superadmin(user: User = Depends(get_current_user)) -> User:
    if user.role != Role.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso restrito a administradores")
    require_admin_mfa(user)
    return user


@dataclass
class WorkspaceContext:
    workspace: Workspace
    role: Role  # effective role in this workspace (owner = admin)
    user: User

    def require(self, role: Role) -> None:
        if not has_role(self.role, role):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permissão insuficiente no workspace")


async def workspace_role(db: AsyncSession, workspace: Workspace, user: User) -> Role | None:
    if workspace.owner_id == user.id:
        return Role.ADMIN
    member = await db.scalar(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == workspace.id, WorkspaceMember.user_id == user.id)
    )
    return member.role if member else None


async def resolve_workspace(db: AsyncSession, user: User, workspace_id: uuid.UUID | None) -> WorkspaceContext:
    candidates = [workspace_id] if workspace_id else [user.active_workspace_id]
    for ws_id in candidates:
        if ws_id is None:
            continue
        workspace = await db.get(Workspace, ws_id)
        if workspace is not None:
            role = await workspace_role(db, workspace, user)
            if role is not None:
                return WorkspaceContext(workspace, role, user)
        if workspace_id:  # explicitly requested but not accessible
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace não encontrado")
    # fallback: first owned, then first membership
    workspace = await db.scalar(
        select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.created_at).limit(1)
    )
    if workspace is not None:
        return WorkspaceContext(workspace, Role.ADMIN, user)
    member = await db.scalar(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user.id).order_by(WorkspaceMember.created_at).limit(1)
    )
    if member is not None:
        return WorkspaceContext(await db.get(Workspace, member.workspace_id), member.role, user)  # type: ignore[arg-type]
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nenhum workspace disponível")


async def get_workspace_context(
    x_workspace_id: uuid.UUID | None = Header(default=None, alias="X-Workspace-ID"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WorkspaceContext:
    """Active workspace: X-Workspace-ID header, else the user's saved active workspace, else the first one."""
    return await resolve_workspace(db, user, x_workspace_id)


async def get_active_workspace(ctx: WorkspaceContext = Depends(get_workspace_context)) -> Workspace:
    return ctx.workspace


def require_workspace_role(role: Role):
    async def _dep(ctx: WorkspaceContext = Depends(get_workspace_context)) -> WorkspaceContext:
        ctx.require(role)
        return ctx

    return _dep


@dataclass
class Pagination:
    limit: int
    offset: int


def pagination(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> Pagination:
    return Pagination(limit, offset)
