import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context, require_workspace_role
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiting import client_ip
from app.core.security import Role, encrypt_api_key
from app.models import Account, AccountStatus, BotConfig, BotState, ExecutionMode
from app.schemas.account import AccountCreate, AccountDashboard, AccountRead, AccountUpdate, AccountVerifyResult
from app.services.accounts import client_for_account
from app.services.audit import log_event
from app.services.binance_client import BinanceAPIError
from app.services.dashboard import build_dashboard
from app.services.dispatch import enqueue
from app.services.execution import is_global_kill_switch_on
from app.services.kill_switch import reset_account_kill_switch
from app.strategy.state_machine import ACTIVE_STATES

router = APIRouter()


async def get_account_in_workspace(account_id: uuid.UUID, ctx: WorkspaceContext, db: AsyncSession) -> Account:
    # Tenant isolation: always filter by the active workspace.
    account = await db.scalar(select(Account).where(Account.id == account_id, Account.workspace_id == ctx.workspace.id))
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conta não encontrada")
    return account


def _check_live_allowed(mode: ExecutionMode | None, ctx: WorkspaceContext) -> None:
    if mode == ExecutionMode.LIVE:
        ctx.require(Role.ADMIN)


@router.get("/", response_model=list[AccountRead])
async def list_accounts(
    ctx: WorkspaceContext = Depends(get_workspace_context), db: AsyncSession = Depends(get_db)
) -> list[Account]:
    result = await db.scalars(
        select(Account).where(Account.workspace_id == ctx.workspace.id).order_by(Account.created_at)
    )
    return list(result)


@router.post("/", response_model=AccountRead, status_code=status.HTTP_201_CREATED)
async def connect_account(
    data: AccountCreate,
    request: Request,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Conecta uma conta Binance. As chaves são criptografadas (AES-256-GCM) antes de salvar."""
    _check_live_allowed(data.execution_mode, ctx)
    aad = str(ctx.workspace.id).encode()  # bind ciphertext to the tenant
    account = Account(
        workspace_id=ctx.workspace.id,
        label=data.label,
        encrypted_api_key=encrypt_api_key(data.api_key, aad),
        encrypted_api_secret=encrypt_api_key(data.api_secret, aad),
        api_key_last4=data.api_key[-4:],
        is_testnet=data.is_testnet,
        execution_mode=data.execution_mode,
        paper_initial_balance=data.paper_initial_balance,
        paper_balance=data.paper_initial_balance,
        max_leverage=data.max_leverage,
        max_risk_per_trade_pct=data.max_risk_per_trade_pct,
        max_daily_loss_pct=data.max_daily_loss_pct,
        max_drawdown_pct=data.max_drawdown_pct,
    )
    db.add(account)
    await db.flush()
    await log_event(
        db, "account.connected", user_id=ctx.user.id, workspace_id=ctx.workspace.id, account_id=account.id,
        ip_address=client_ip(request), label=data.label, testnet=data.is_testnet, mode=data.execution_mode,
    )  # fmt: skip
    await db.commit()
    await db.refresh(account)
    return account


@router.get("/{account_id}", response_model=AccountRead)
async def get_account(
    account_id: uuid.UUID, ctx: WorkspaceContext = Depends(get_workspace_context), db: AsyncSession = Depends(get_db)
) -> Account:
    return await get_account_in_workspace(account_id, ctx, db)


@router.patch("/{account_id}", response_model=AccountRead)
async def update_account(
    account_id: uuid.UUID,
    data: AccountUpdate,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> Account:
    account = await get_account_in_workspace(account_id, ctx, db)
    changes = data.model_dump(exclude_unset=True)
    if ("execution_mode" in changes or "is_testnet" in changes) and account.status == AccountStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Pare a conta antes de mudar o modo de execução")
    _check_live_allowed(changes.get("execution_mode"), ctx)
    for key, value in changes.items():
        setattr(account, key, value)
    await log_event(
        db, "account.updated", user_id=ctx.user.id, workspace_id=ctx.workspace.id, account_id=account.id,
        changes=changes,
    )  # fmt: skip
    await db.commit()
    await db.refresh(account)
    return account


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> None:
    account = await get_account_in_workspace(account_id, ctx, db)
    if account.status == AccountStatus.RUNNING:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Pare a conta antes de removê-la")
    await log_event(db, "account.deleted", user_id=ctx.user.id, workspace_id=ctx.workspace.id, label=account.label)
    await db.delete(account)
    await db.commit()


@router.get("/{account_id}/dashboard", response_model=AccountDashboard)
async def account_dashboard(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
) -> AccountDashboard:
    account = await get_account_in_workspace(account_id, ctx, db)
    client = client_for_account(account) if account.execution_mode == ExecutionMode.LIVE else None
    try:
        return AccountDashboard(**(await build_dashboard(db, account, client)))
    finally:
        if client is not None:
            await client.close()


@router.post("/{account_id}/verify", response_model=AccountVerifyResult)
async def verify_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> AccountVerifyResult:
    """Testa as chaves na Binance (somente leitura: GET /fapi/v2/account)."""
    account = await get_account_in_workspace(account_id, ctx, db)
    try:
        async with client_for_account(account) as client:
            data = await client.get_account()
    except BinanceAPIError as exc:
        return AccountVerifyResult(ok=False, error=exc.message)
    except Exception as exc:
        return AccountVerifyResult(ok=False, error=str(exc)[:200])
    return AccountVerifyResult(ok=True, can_trade=data.get("canTrade"), wallet_balance=data.get("totalWalletBalance"))


@router.post("/{account_id}/start", response_model=AccountRead)
async def start_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Coloca a conta em execução e inicia todos os bots habilitados dela."""
    account = await get_account_in_workspace(account_id, ctx, db)
    if account.status == AccountStatus.KILLED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Kill switch ativo; revise o risco e faça o reset")
    if await is_global_kill_switch_on(db):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Kill switch global ativo")
    if account.execution_mode == ExecutionMode.LIVE and not settings.LIVE_TRADING_ENABLED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Trading real desabilitado no servidor")
    account.status = AccountStatus.RUNNING
    account.status_reason = None
    started: list[str] = []
    for bot in await db.scalars(select(BotConfig).where(BotConfig.account_id == account.id)):
        if bot.is_enabled and bot.state in (BotState.IDLE, BotState.STOPPED, BotState.ERROR):
            bot.state = BotState.STARTING
            bot.last_error = None
            started.append(str(bot.id))
    await log_event(
        db, "account.started", user_id=ctx.user.id, workspace_id=ctx.workspace.id, account_id=account.id,
        bots=started,
    )  # fmt: skip
    await db.commit()
    for bot_id in started:
        enqueue("bots.start", bot_id)
    await db.refresh(account)
    return account


@router.post("/{account_id}/stop", response_model=AccountRead)
async def stop_account(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> Account:
    """Para a conta e todos os bots (posições abertas permanecem, protegidas pelos stops)."""
    account = await get_account_in_workspace(account_id, ctx, db)
    if account.status != AccountStatus.KILLED:
        account.status = AccountStatus.STOPPED
    for bot in await db.scalars(select(BotConfig).where(BotConfig.account_id == account.id)):
        if bot.state in ACTIVE_STATES:
            bot.state = BotState.STOPPED
    await log_event(db, "account.stopped", user_id=ctx.user.id, workspace_id=ctx.workspace.id, account_id=account.id)
    await db.commit()
    await db.refresh(account)
    return account


@router.post("/{account_id}/reset-kill-switch", response_model=AccountRead)
async def reset_kill_switch(
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> Account:
    account = await get_account_in_workspace(account_id, ctx, db)
    if account.status != AccountStatus.KILLED:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Kill switch não está ativo")
    await reset_account_kill_switch(db, account, ctx.user.id)
    await db.commit()
    await db.refresh(account)
    return account
