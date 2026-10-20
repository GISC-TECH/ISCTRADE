import pytest
from cryptography.exceptions import InvalidTag

from app.core.security import (
    REFRESH_TOKEN_TYPE,
    Role,
    TokenError,
    create_access_token,
    create_refresh_token,
    decrypt_api_key,
    encrypt_api_key,
    has_role,
    hash_password,
    verify_password,
    verify_token,
)


def test_password_hashing() -> None:
    h = hash_password("s3nha-de-teste")
    assert h != "s3nha-de-teste"
    assert verify_password("s3nha-de-teste", h)
    assert not verify_password("errada", h)


def test_jwt_roundtrip_and_type_check() -> None:
    access = create_access_token("user-1")
    assert verify_token(access)["sub"] == "user-1"
    refresh = create_refresh_token("user-1")
    assert verify_token(refresh, REFRESH_TOKEN_TYPE)["sub"] == "user-1"
    with pytest.raises(TokenError):
        verify_token(refresh)  # refresh token is not an access token


def test_aes_gcm_roundtrip_with_random_nonce() -> None:
    a = encrypt_api_key("placeholder-api-key", b"ws-1")
    b = encrypt_api_key("placeholder-api-key", b"ws-1")
    assert a != b  # random nonce
    assert decrypt_api_key(a, b"ws-1") == "placeholder-api-key"
    with pytest.raises(InvalidTag):
        decrypt_api_key(a, b"ws-2")  # bound to tenant


def test_rbac() -> None:
    assert has_role(Role.ADMIN, Role.TRADER)
    assert not has_role(Role.VIEWER, Role.TRADER)
