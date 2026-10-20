"""Workspaces are the tenant boundary: every account, bot, order and trade belongs to one workspace."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.security import Role
from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, str_enum

if TYPE_CHECKING:
    from app.models.account import Account
    from app.models.user import User


class Workspace(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)

    owner: Mapped[User] = relationship(back_populates="owned_workspaces", foreign_keys=[owner_id])
    members: Mapped[list[WorkspaceMember]] = relationship(back_populates="workspace", cascade="all, delete-orphan")
    accounts: Mapped[list[Account]] = relationship(back_populates="workspace", cascade="all, delete-orphan")


class WorkspaceMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Membership of a user in a workspace. The owner is implicitly ADMIN and has no row here."""

    __tablename__ = "workspace_members"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    role: Mapped[Role] = mapped_column(str_enum(Role), default=Role.VIEWER, nullable=False)

    workspace: Mapped[Workspace] = relationship(back_populates="members")
    user: Mapped[User] = relationship(back_populates="memberships")
