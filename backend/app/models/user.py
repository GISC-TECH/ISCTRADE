from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, false
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
    # TOTP 2FA (RFC 6238). Secrets are AES-256-GCM encrypted with MASTER_ENCRYPTION_KEY (AAD bound to the user id);
    # recovery codes are stored only as salted SHA-256 hashes and removed when used.
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false(), nullable=False)
    totp_secret_enc: Mapped[str | None] = mapped_column(Text)
    totp_pending_secret_enc: Mapped[str | None] = mapped_column(Text)  # enrolment started, not yet confirmed
    totp_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    totp_last_step: Mapped[int | None] = mapped_column(BigInteger)  # last accepted time step (replay protection)
    totp_recovery_codes: Mapped[list[Any]] = mapped_column(JSON, default=list, server_default="[]", nullable=False)
    # Regulatory/tax disclaimer acknowledgement (version shown in the UI on first login).
    disclaimer_version: Mapped[str | None] = mapped_column(String(20))
    disclaimer_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("workspaces.id", ondelete="SET NULL", use_alter=True), nullable=True
    )

    owned_workspaces: Mapped[list[Workspace]] = relationship(back_populates="owner", foreign_keys="Workspace.owner_id")
    memberships: Mapped[list[WorkspaceMember]] = relationship(back_populates="user", cascade="all, delete-orphan")
