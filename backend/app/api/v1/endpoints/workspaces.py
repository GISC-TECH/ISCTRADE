from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.workspace import WorkspaceCreate, WorkspaceRead

router = APIRouter()


@router.get("/", response_model=list[WorkspaceRead])
async def list_workspaces(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # TODO: incluir workspaces em que o usuário é membro.
    return list(await db.scalars(select(Workspace).where(Workspace.owner_id == user.id)))


@router.post("/", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    data: WorkspaceCreate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    ws = Workspace(name=data.name, owner_id=user.id)
    db.add(ws)
    await db.commit()
    await db.refresh(ws)
    return ws
