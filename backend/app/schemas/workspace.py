import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import Role


class WorkspaceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class WorkspaceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    created_at: datetime
    role: Role | None = None
    is_active: bool = False


class MemberAdd(BaseModel):
    email: EmailStr
    role: Role = Role.VIEWER


class MemberUpdate(BaseModel):
    role: Role


class MemberRead(BaseModel):
    user_id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    role: Role
    is_owner: bool = False
