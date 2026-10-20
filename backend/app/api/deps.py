"""Shared API dependencies: current user and active workspace (tenant isolation)."""

import uuid

from fastapi import Cookie, Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import TokenError, verify_token
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMember

ACCESS_COOKIE_NAME = "access_token"
_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    db: AsyncSession = Depends(get_db),
    access_token: str | None = Cookie(default=None, alias=ACCESS_COOKIE_NAME),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> User:
    token = access_token or (credentials.credentials if credentials else None)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Não autenticado")
    try:
        payload = verify_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (TokenError, KeyError, ValueError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Usuário inválido")
    return user


async def get_active_workspace(
    x_workspace_id: uuid.UUID = Header(..., alias="X-Workspace-ID"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Workspace:
    """Resolve the workspace from the X-Workspace-ID header and ensure the user is owner or member."""
    stmt = (
        select(Workspace)
        .outerjoin(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id == x_workspace_id)
        .where(or_(Workspace.owner_id == user.id, WorkspaceMember.user_id == user.id))
    )
    workspace = (await db.execute(stmt)).scalars().first()
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace não encontrado")
    return workspace
