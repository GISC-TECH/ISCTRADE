import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.security import Role


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(None, max_length=255)
    # Required when REGISTRATION_MODE=invite (single-use token from an admin).
    invite_token: str | None = Field(None, max_length=128)


class UserLogin(BaseModel):
    email: EmailStr
    password: str = Field(max_length=128)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str | None = None
    role: Role
    is_active: bool
    active_workspace_id: uuid.UUID | None = None
    created_at: datetime
    totp_enabled: bool = False
    # Computed per request (see app.services.users.user_read): 2FA mandatory for this user and why
    # ("policy" | "live" | "admin"), and whether the current disclaimer still needs acknowledgement.
    mfa_required: bool = False
    mfa_reason: str | None = None
    disclaimer_version: str | None = None
    disclaimer_pending: bool = False


class UserUpdate(BaseModel):
    full_name: str | None = Field(None, max_length=255)


class PasswordChange(BaseModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class MfaChallenge(BaseModel):
    """Login step-up: password was correct, a TOTP/recovery code is still needed (POST /auth/login/2fa)."""

    mfa_required: bool = True
    mfa_token: str
    expires_in: int


class MfaLoginVerify(BaseModel):
    mfa_token: str = Field(max_length=2048)
    code: str = Field(min_length=6, max_length=20)


class MfaCode(BaseModel):
    code: str = Field(min_length=6, max_length=20)


class MfaPasswordCode(BaseModel):
    password: str = Field(max_length=128)
    code: str = Field(min_length=6, max_length=20)


class MfaSetupOut(BaseModel):
    secret: str
    otpauth_uri: str
    qr_svg: str


class MfaStatusOut(BaseModel):
    enabled: bool
    pending: bool
    enabled_at: datetime | None = None
    recovery_codes_remaining: int
    required: bool
    required_reason: str | None = None
    live_requires_2fa: bool


class RecoveryCodesOut(BaseModel):
    recovery_codes: list[str]


class DisclaimerAccept(BaseModel):
    version: str = Field(max_length=20)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserRead


class AdminUserUpdate(BaseModel):
    role: Role | None = None
    is_active: bool | None = None
