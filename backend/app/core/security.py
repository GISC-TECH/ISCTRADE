"""Security helpers: password hashing, JWT, AES-256-GCM encryption and RBAC."""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import anyio
import bcrypt
import jwt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
MFA_TOKEN_TYPE = "mfa"  # short-lived login step-up token (password ok, TOTP pending)
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


async def hash_password_async(password: str) -> str:
    """bcrypt in a worker thread: ~0.2 s of CPU per call must not stall the event loop (WS, other requests)."""
    return await anyio.to_thread.run_sync(hash_password, password)


async def verify_password_async(password: str, hashed: str) -> bool:
    return await anyio.to_thread.run_sync(verify_password, password, hashed)


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


def create_mfa_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    return _create_token(subject, MFA_TOKEN_TYPE, timedelta(minutes=settings.TOTP_CHALLENGE_MINUTES), extra)


def verify_token(token: str, expected_type: str = ACCESS_TOKEN_TYPE) -> dict[str, Any]:
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenError(str(exc)) from exc
    if payload.get("type") != expected_type:
        raise TokenError("invalid token type")
    return payload


# --------------------------------------------------------------------------- #
# AES-256-GCM encryption of exchange credentials / TOTP secrets, with a keyring
# --------------------------------------------------------------------------- #
# Ciphertext format:
#   v2:<key id>:base64(nonce || ciphertext+tag)   (current; key id = first 8 hex of SHA-256(key), not secret)
#   base64(nonce || ciphertext+tag)              (legacy, before key rotation support; tried with every key)
# Keys: MASTER_ENCRYPTION_KEY encrypts; MASTER_ENCRYPTION_KEYS_PREVIOUS (comma-separated) only decrypt.
_CIPHER_VERSION = "v2"


class KeyringError(RuntimeError):
    """Ciphertext encrypted with a key that is not in the keyring (or malformed)."""


def _decode_key(value: str, name: str) -> bytes:
    """Decode a base64 32-byte AES key.

    Generate with: python -m app.keys_cli generate
    """
    try:
        key = base64.b64decode(value.strip(), validate=True)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"{name} must be base64-encoded") from exc
    if len(key) != 32:
        raise RuntimeError(f"{name} must decode to exactly 32 bytes (AES-256)")
    return key


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def _master_key() -> bytes:
    return _decode_key(settings.MASTER_ENCRYPTION_KEY, "MASTER_ENCRYPTION_KEY")


def keyring() -> dict[str, bytes]:
    """Key id -> key; the current key first, then MASTER_ENCRYPTION_KEYS_PREVIOUS in the given order."""
    ring = {}
    current = _master_key()
    ring[key_id(current)] = current
    for raw in (settings.MASTER_ENCRYPTION_KEYS_PREVIOUS or "").split(","):
        if raw.strip():
            key = _decode_key(raw, "MASTER_ENCRYPTION_KEYS_PREVIOUS")
            ring.setdefault(key_id(key), key)
    return ring


def current_key_id() -> str:
    return key_id(_master_key())


def ciphertext_key_id(token: str) -> str | None:
    """Key id stamped in a v2 ciphertext; None for legacy ciphertexts."""
    if token.startswith(_CIPHER_VERSION + ":"):
        parts = token.split(":", 2)
        if len(parts) == 3:
            return parts[1]
    return None


def needs_reencryption(token: str) -> bool:
    return ciphertext_key_id(token) != current_key_id()


def encrypt_api_key(plaintext: str, associated_data: bytes | None = None) -> str:
    """Encrypt with AES-256-GCM under the current key. Output: v2:<key id>:base64(nonce || ciphertext+tag)."""
    key = _master_key()
    nonce = os.urandom(_NONCE_SIZE)
    ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), associated_data)
    return f"{_CIPHER_VERSION}:{key_id(key)}:{base64.b64encode(nonce + ct).decode('ascii')}"


def _open(key: bytes, payload: str, associated_data: bytes | None) -> str:
    raw = base64.b64decode(payload)
    nonce, ct = raw[:_NONCE_SIZE], raw[_NONCE_SIZE:]
    return AESGCM(key).decrypt(nonce, ct, associated_data).decode("utf-8")


def decrypt_api_key(token: str, associated_data: bytes | None = None) -> str:
    ring = keyring()
    kid = ciphertext_key_id(token)
    if kid is not None:
        key = ring.get(kid)
        if key is None:
            raise KeyringError(
                f"ciphertext uses key id {kid}, which is not in MASTER_ENCRYPTION_KEY/MASTER_ENCRYPTION_KEYS_PREVIOUS"
            )
        return _open(key, token.split(":", 2)[2], associated_data)
    last: Exception | None = None
    for key in ring.values():  # legacy ciphertext: the GCM tag tells which key is right
        try:
            return _open(key, token, associated_data)
        except InvalidTag as exc:
            last = exc
    raise last or KeyringError("empty keyring")


def reencrypt(token: str, associated_data: bytes | None = None) -> str:
    """Decrypt with whichever key matches and encrypt again under the current key."""
    return encrypt_api_key(decrypt_api_key(token, associated_data), associated_data)


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
