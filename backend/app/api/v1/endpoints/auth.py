import logging
import uuid
from datetime import timedelta

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_COOKIE_NAME, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiting import client_ip, rate_limiter
from app.core.rate_limiting import hit as rate_hit
from app.core.rate_limiting import peek as rate_peek
from app.core.security import (
    REFRESH_TOKEN_TYPE,
    Role,
    TokenError,
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
    verify_token,
)
from app.models.base import as_utc, utcnow
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.user import PasswordChange, TokenResponse, UserCreate, UserLogin, UserRead, UserUpdate
from app.services.audit import log_event

logger = logging.getLogger(__name__)
router = APIRouter()
REFRESH_COOKIE_NAME = "refresh_token"
# Non-sensitive marker readable by the Next.js middleware (the refresh cookie is path-scoped to the API).
SESSION_MARKER_COOKIE = "btp_session"
REFRESH_PATH = f"{settings.API_V1_PREFIX}/auth"
LOGIN_FAILURE_WINDOW = 15 * 60
_DUMMY_HASH = hash_password("timing-equalizer-not-a-real-password")


def _cookie_opts() -> dict:
    return dict(httponly=True, secure=settings.COOKIE_SECURE, samesite="lax", domain=settings.COOKIE_DOMAIN or None)


async def issue_tokens(
    response: Response, user: User, db: AsyncSession, family: str | None = None, parent: RefreshToken | None = None
) -> TokenResponse:
    """Set a new access/refresh pair. Every refresh token is recorded (single use) under its login family."""
    jti, family = uuid.uuid4().hex, family or uuid.uuid4().hex
    now = utcnow()
    if parent is not None:
        parent.used_at, parent.replaced_by = parent.used_at or now, jti
    db.add(RefreshToken(
        jti=jti, user_id=user.id, family_id=family, created_at=now,
        expires_at=now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
    ))  # fmt: skip
    # Housekeeping: drop this user's expired rows.
    await db.execute(
        delete(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.expires_at < now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    access = create_access_token(str(user.id), {"role": user.role.value, "ver": user.token_version})
    refresh = create_refresh_token(str(user.id), {"ver": user.token_version, "jti": jti, "fam": family})
    opts = _cookie_opts()
    response.set_cookie(ACCESS_COOKIE_NAME, access, max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60, path="/", **opts)
    response.set_cookie(
        REFRESH_COOKIE_NAME, refresh, max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400, path=REFRESH_PATH, **opts
    )
    response.set_cookie(
        SESSION_MARKER_COOKIE,
        "1",
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path="/",
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        domain=settings.COOKIE_DOMAIN or None,
    )
    return TokenResponse(
        access_token=access,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserRead.model_validate(user),
    )


def clear_tokens(response: Response) -> None:
    opts = {"domain": settings.COOKIE_DOMAIN or None}
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/", **opts)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_PATH, **opts)
    response.delete_cookie(SESSION_MARKER_COOKIE, path="/", **opts)


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(settings.REGISTER_RATE_LIMIT_PER_MINUTE, scope="register"))],
)
async def register(data: UserCreate, request: Request, db: AsyncSession = Depends(get_db)) -> User:
    if not settings.ALLOW_REGISTRATION:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cadastro desabilitado")
    email = data.email.lower()
    if await db.scalar(select(User).where(User.email == email)):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="E-mail já cadastrado")
    # Self-hosted bootstrap: the very first user becomes the platform super-admin.
    is_first = (await db.scalar(select(func.count()).select_from(User))) == 0
    user = User(
        email=email,
        full_name=data.full_name,
        hashed_password=hash_password(data.password),
        role=Role.ADMIN if is_first else Role.TRADER,
    )
    db.add(user)
    await db.flush()
    workspace = Workspace(name="Workspace pessoal", owner_id=user.id)
    db.add(workspace)
    await db.flush()
    user.active_workspace_id = workspace.id
    await log_event(
        db, "auth.register", user_id=user.id, workspace_id=workspace.id, ip_address=client_ip(request),
        superadmin=is_first,
    )  # fmt: skip
    await db.commit()
    await db.refresh(user)
    return user


@router.post(
    "/login",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limiter(settings.LOGIN_RATE_LIMIT_PER_MINUTE, scope="login"))],
)
async def login(
    data: UserLogin, request: Request, response: Response, db: AsyncSession = Depends(get_db)
) -> TokenResponse:
    email = data.email.lower()
    failures_key = f"rl:login-fail:{email}"
    if await rate_peek(failures_key, LOGIN_FAILURE_WINDOW) >= settings.LOGIN_FAILURES_PER_EMAIL:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Muitas tentativas para este e-mail. Tente novamente mais tarde.",
            headers={"Retry-After": str(LOGIN_FAILURE_WINDOW)},
        )
    user = await db.scalar(select(User).where(User.email == email))
    # Always run bcrypt (dummy hash for unknown e-mails) so response time does not reveal registered accounts.
    password_ok = verify_password(data.password, user.hashed_password if user else _DUMMY_HASH)
    if not user or not password_ok or not user.is_active:
        await rate_hit(failures_key, LOGIN_FAILURE_WINDOW)
        await log_event(
            db, "auth.login_failed", level="warning", ip_address=client_ip(request), email=email, commit=True,
        )  # fmt: skip
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")
    await log_event(db, "auth.login", user_id=user.id, ip_address=client_ip(request), commit=True)
    return await issue_tokens(response, user, db)


async def _revoke_family(db: AsyncSession, family: str) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
        .execution_options(synchronize_session=False)
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange the httpOnly refresh cookie for a new pair. Refresh tokens are single use: presenting one that was
    already exchanged (outside a short grace window for concurrent tabs) is treated as theft and revokes the whole
    login family, logging out both the attacker and the victim."""
    if not refresh_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão expirada")
    try:
        payload = verify_token(refresh_token, REFRESH_TOKEN_TYPE)
        user = await db.get(User, uuid.UUID(payload["sub"]))
        jti, family = str(payload["jti"]), payload.get("fam")
    except (TokenError, KeyError, ValueError):
        clear_tokens(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão expirada") from None
    if user is None or not user.is_active or payload.get("ver", 0) != user.token_version:
        clear_tokens(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão revogada")
    if family is None:  # issued before the ledger existed: accept once and start a tracked family
        return await issue_tokens(response, user, db)

    row = await db.scalar(select(RefreshToken).where(RefreshToken.jti == jti).with_for_update())
    if row is None or row.user_id != user.id or row.family_id != family or row.revoked_at is not None:
        clear_tokens(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão revogada")
    now = utcnow()
    if row.used_at is not None:
        if now - as_utc(row.used_at) <= timedelta(seconds=settings.REFRESH_REUSE_GRACE_SECONDS):
            return await issue_tokens(response, user, db, family)  # concurrent refresh from another tab
        await _revoke_family(db, family)
        await log_event(
            db, "auth.refresh_reuse_detected", level="critical", user_id=user.id, ip_address=client_ip(request),
            family=family,
        )  # fmt: skip
        await db.commit()
        clear_tokens(response)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sessão revogada")
    return await issue_tokens(response, user, db, family, parent=row)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    response: Response,
    refresh_token: str | None = Cookie(default=None, alias=REFRESH_COOKIE_NAME),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Clear the cookies and revoke this login's refresh family server-side."""
    if refresh_token:
        try:
            family = verify_token(refresh_token, REFRESH_TOKEN_TYPE).get("fam")
        except TokenError:
            family = None
        if family:
            await _revoke_family(db, str(family))
            await db.commit()
    clear_tokens(response)


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(response: Response, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Revokes every session of the user (all refresh/access tokens)."""
    user.token_version += 1
    await log_event(db, "auth.logout_all", user_id=user.id)
    await db.commit()
    clear_tokens(response)


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(get_current_user)) -> User:
    return user


@router.patch("/me", response_model=UserRead)
async def update_me(data: UserUpdate, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    for key, value in data.model_dump(exclude_unset=True).items():
        setattr(user, key, value)
    await db.commit()
    await db.refresh(user)
    return user


@router.post(
    "/me/password",
    response_model=TokenResponse,
    dependencies=[Depends(rate_limiter(settings.LOGIN_RATE_LIMIT_PER_MINUTE, scope="password"))],
)
async def change_password(
    data: PasswordChange,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    if not verify_password(data.current_password, user.hashed_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Senha atual incorreta")
    user.hashed_password = hash_password(data.new_password)
    user.token_version += 1  # revoke other sessions
    await log_event(db, "auth.password_changed", user_id=user.id)
    await db.commit()
    await db.refresh(user)
    return await issue_tokens(response, user, db)
