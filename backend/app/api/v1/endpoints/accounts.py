import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_active_workspace, get_current_user
from app.core.database import get_db
from app.core.security import Role, encrypt_api_key, has_role
from app.models.account import Account, AccountStatus
from app.models.user import User
from app.models.workspace import Workspace
from app.schemas.account import AccountCreate, AccountDashboard, AccountRead
from app.services.audit import log_event

router = APIRouter()


async def _get_account(account_id: uuid.UUID, workspace: Workspace, db: AsyncSession) -> Account:
    # Tenant isolation: always filter by the active workspace.
    account = await db.scalar(select(Account).where(Account.id == account_id, Account.workspace_id == workspace.id))
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada")
    return account


def _require_trader(user: User) -> None:
    if not has_role(user.role, Role.TRADER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Permissão insuficiente")


@router.get("/", response_model=list[AccountRead])
async def list_accounts(
    workspace: Workspace = Depends(get_active_workspace), db: AsyncSession = Depends(get_db)
) -> list[Account]:
    result = await db.scalars(select(Account).where(Account.workspace_id == workspace.id).order_by(Account.created_at))
    return list(result)


@router.post("/", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
async def connect_account(
    data: AccountCreate,
    user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_active_workspace),
    db: AsyncSession = Depends(get_db),
) -> Account:
    _require_trader(user)
    aad = str(workspace.id).encode()  # bind ciphertext to the tenant
    account = Account(
        workspace_id=workspace.id,
        label=data.label,
        encrypted_api_key=encrypt_api_key(data.api_key, aad),
        encrypted_api_secret=encrypt_api_key(data.api_secret, aad),
        api_key_last4=data.api_key[-4:],
        is_testnet=data.is_testnet,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)
    await log_event("account.connected", user_id=user.id, workspace_id=workspace.id, account_id=account.id)
    return account


@router.get("/{account_id}/dashboard", response_model=AccountDashboard)
async def account_dashboard(
    account_id: uuid.UUID,
    workspace: Workspace = Depends(get_active_workspace),
    db: AsyncSession = Depends(get_db),
) -> AccountDashboard:
    account = await _get_account(account_id, workspace, db)
    # TODO: buscar saldo/posições reais via BinanceFuturesClient (cacheado no Redis).
    return AccountDashboard(account_id=account.id, status=account.status)


@router.post("/{account_id}/start", response_model=AccountRead)
async def start_account(
    account_id: uuid.UUID,
    user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_active_workspace),
    db: AsyncSession = Depends(get_db),
) -> Account:
    _require_trader(user)
    account = await _get_account(account_id, workspace, db)
    if account.status == AccountStatus.KILLED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Kill switch ativo; revise o risco antes")
    account.status = AccountStatus.RUNNING
    await db.commit()
    await db.refresh(account)
    # TODO: disparar task Celery que inicia o bot (app.tasks.bot_tasks.start_bot).
    await log_event("account.started", user_id=user.id, workspace_id=workspace.id, account_id=account.id)
    return account


@router.post("/{account_id}/stop", response_model=AccountRead)
async def stop_account(
    account_id: uuid.UUID,
    user: User = Depends(get_current_user),
    workspace: Workspace = Depends(get_active_workspace),
    db: AsyncSession = Depends(get_db),
) -> Account:
    _require_trader(user)
    account = await _get_account(account_id, workspace, db)
    account.status = AccountStatus.STOPPED
    await db.commit()
    await db.refresh(account)
    await log_event("account.stopped", user_id=user.id, workspace_id=workspace.id, account_id=account.id)
    return account
