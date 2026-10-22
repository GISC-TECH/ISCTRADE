"""Helpers to render the current user with computed security fields."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.legal import DISCLAIMER_VERSION
from app.models.user import User
from app.schemas.user import UserRead
from app.services.mfa import mfa_required_reason


async def user_read(db: AsyncSession, user: User) -> UserRead:
    out = UserRead.model_validate(user)
    reason = await mfa_required_reason(db, user)
    out.mfa_required = reason is not None
    out.mfa_reason = reason
    out.disclaimer_pending = user.disclaimer_version != DISCLAIMER_VERSION
    return out
