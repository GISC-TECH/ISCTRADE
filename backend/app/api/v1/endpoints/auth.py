from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import ACCESS_COOKIE_NAME, get_current_user
from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, create_refresh_token, hash_password, verify_password
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.user import TokenResponse, UserCreate, UserLogin, UserRead

router = APIRouter()
REFRESH_COOKIE_NAME = "refresh_token"


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(data: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    existing = await db.scalar(select(User).where(User.email == data.email.lower()))
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="E-mail já cadastrado")
    user = User(email=data.email.lower(), full_name=data.full_name, hashed_password=hash_password(data.password))
    db.add(user)
    await db.flush()
    # Every new user gets a personal workspace (tenant).
    db.add(Workspace(name="Workspace pessoal", owner_id=user.id))
    await db.commit()
    await db.refresh(user)
    return user


@router.post("/login", response_model=TokenResponse)
async def login(data: UserLogin, response: Response, db: AsyncSession = Depends(get_db)) -> TokenResponse:
    user = await db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not verify_password(data.password, user.hashed_password) or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciais inválidas")

    access = create_access_token(str(user.id), {"role": user.role.value, "ver": user.token_version})
    refresh = create_refresh_token(str(user.id))
    common = dict(httponly=True, secure=settings.COOKIE_SECURE, samesite="lax", domain=settings.COOKIE_DOMAIN)
    response.set_cookie(ACCESS_COOKIE_NAME, access, max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60, **common)
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        refresh,
        max_age=settings.REFRESH_TOKEN_EXPIRE_DAYS * 86400,
        path=f"{settings.API_V1_PREFIX}/auth",
        **common,
    )
    return TokenResponse(access_token=access)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE_NAME)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=f"{settings.API_V1_PREFIX}/auth")


@router.get("/me", response_model=UserRead)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
