from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import Role
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from app.models.workspace import Workspace, WorkspaceMember


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255))
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    # Global role: ADMIN = platform super-admin. Workspace permissions live in WorkspaceMember.
    role: Mapped[Role] = mapped_column(str_enum(Role), default=Role.TRADER, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # Incremented to revoke every refresh token of the user (logout everywhere / password change).
    token_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    active_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL", use_alter=True), nullable=True
    )

    owned_workspaces: Mapped[list[Workspace]] = relationship(back_populates="owner", foreign_keys="Workspace.owner_id")
    memberships: Mapped[list[WorkspaceMember]] = relationship(back_populates="user", cascade="all, delete-orphan")
