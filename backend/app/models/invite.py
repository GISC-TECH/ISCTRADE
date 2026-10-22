"""Single-use registration invites (REGISTRATION_MODE=invite). Only the SHA-256 of the token is stored."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class RegistrationInvite(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "registration_invites"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Optional: the invite only works for this e-mail (lower-case).
    email: Mapped[str | None] = mapped_column(String(255))
    note: Mapped[str | None] = mapped_column(String(255))
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    used_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
