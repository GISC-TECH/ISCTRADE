"""Security helpers: password hashing, JWT, AES-256-GCM encryption and RBAC."""

from __future__ import annotations

import base64
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import bcrypt
import jwt
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
_NONCE_SIZE = 12  # 96-bit nonce recommended for GCM


# --------------------------------------------------------------------------- #
# Password hashing (bcrypt)
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# JWT (PyJWT)
# --------------------------------------------------------------------------- #
class TokenError(Exception):
    """Raised when a token is invalid, expired or of the wrong type."""


def _create_token(subject: str, token_type: str, expires_delta: timedelta, extra: dict[str, Any] | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "type": token_type,
        "iat": now,
        "exp": now + expires_delta,
        "jti": uuid.uuid4().hex,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    return _create_token(subject, ACCESS_TOKEN_TYPE, timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES), extra)


def create_refresh_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    return _create_token(subject, REFRESH_TOKEN_TYPE, timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS), extra)


def verify_token(token: str, expected_type: str = ACCESS_TOKEN_TYPE) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("type") != expected_type:
        raise TokenError("invalid token type")
    return payload


# --------------------------------------------------------------------------- #
# AES-256-GCM encryption of Binance API credentials
# --------------------------------------------------------------------------- #
def _master_key() -> bytes:
    """Decode MASTER_ENCRYPTION_KEY (base64 of 32 random bytes).

    Generate with: python -c "import os,base64;print(base64.b64encode(os.urandom(32)).decode())"
    """
    try:
        key = base64.b64decode(settings.MASTER_ENCRYPTION_KEY, validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("MASTER_ENCRYPTION_KEY must be base64-encoded") from exc
    if len(key) != 32:
        raise RuntimeError("MASTER_ENCRYPTION_KEY must decode to exactly 32 bytes (AES-256)")
    return key


def encrypt_api_key(plaintext: str, associated_data: bytes | None = None) -> str:
    """Encrypt with AES-256-GCM. Output: base64(nonce || ciphertext+tag)."""
    nonce = os.urandom(_NONCE_SIZE)
    ct = AESGCM(_master_key()).encrypt(nonce, plaintext.encode("utf-8"), associated_data)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt_api_key(token: str, associated_data: bytes | None = None) -> str:
    raw = base64.b64decode(token)
    nonce, ct = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
    return AESGCM(_master_key()).decrypt(nonce, ct, associated_data).decode("utf-8")


# --------------------------------------------------------------------------- #
# RBAC
# --------------------------------------------------------------------------- #
class Role(str, Enum):
    ADMIN = "admin"
    TRADER = "trader"
    VIEWER = "viewer"


ROLE_HIERARCHY: dict[Role, int] = {Role.VIEWER: 0, Role.TRADER: 1, Role.ADMIN: 2}


def has_role(user_role: Role | str, required: Role) -> bool:
    return ROLE_HIERARCHY[Role(user_role)] >= ROLE_HIERARCHY[required]


def require_role(required: Role):
    """FastAPI dependency factory: `Depends(require_role(Role.ADMIN))`."""
    from fastapi import Depends, HTTPException, status

    from app.api.deps import get_current_user

    async def _checker(user=Depends(get_current_user)):
        if not has_role(user.role, required):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permissão insuficiente")
        return user

    return _checker
