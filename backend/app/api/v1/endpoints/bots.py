import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context, require_workspace_role
from app.api.v1.endpoints.accounts import get_account_in_workspace
from app.core.config import settings
from app.core.database import get_db
from app.core.security import Role
from app.models import AccountStatus, BotConfig, BotState, ExecutionMode
from app.schemas.bot import BotCreate, BotRead, BotUpdate
from app.services.audit import log_event
from app.services.dispatch import enqueue
from app.services.execution import is_global_kill_switch_on
from app.services.mfa import require_live_mfa
from app.strategy.base import StrategyParamError
from app.strategy.state_machine import ACTIVE_STATES, BotStateMachine, InvalidTransition
from app.strategy.strategies import get_strategy_class

router = APIRouter()


async def _get_bot(bot_id: uuid.UUID, ctx: WorkspaceContext, db: AsyncSession) -> BotConfig:
    bot = await db.scalar(select(BotConfig).where(BotConfig.id == bot_id, BotConfig.workspace_id == ctx.workspace.id))
    if bot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Bot não encontrado")
    return bot


def _validate(strategy_key: str, params: dict) -> dict:
    try:
        return get_strategy_class(strategy_key).validate_params(params)
    except StrategyParamError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc


@router.get("/", response_model=list[BotRead])
async def list_bots(
    account_id: uuid.UUID | None = Query(None),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(BotConfig).where(BotConfig.workspace_id == ctx.workspace.id)
    if account_id:
        stmt = stmt.where(BotConfig.account_id == account_id)
    return list(await db.scalars(stmt.order_by(BotConfig.created_at)))


@router.post("/", response_model=BotRead, status_code=status.HTTP_201_CREATED)
async def create_bot(
    data: BotCreate,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
):
    account = await get_account_in_workspace(data.account_id, ctx, db)
    params = _validate(data.strategy_key, data.params)
    if data.leverage > account.max_leverage:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Alavancagem acima do limite da conta")
    bot = BotConfig(
        workspace_id=ctx.workspace.id,
        account_id=account.id,
        name=data.name,
        strategy_key=data.strategy_key,
        symbol=data.symbol,
        interval=data.interval,
        leverage=data.leverage,
        params=params,
        options=data.options.model_dump(),
        state=BotState.IDLE,
        runtime={},
    )
    db.add(bot)
    await db.flush()
    await log_event(db, "bot.created", user_id=ctx.user.id, workspace_id=ctx.workspace.id, account_id=account.id,
                    bot_id=bot.id, strategy=data.strategy_key, symbol=data.symbol)  # fmt: skip
    await db.commit()
    await db.refresh(bot)
    return bot


@router.get("/{bot_id}", response_model=BotRead)
async def get_bot(bot_id: uuid.UUID, ctx: WorkspaceContext = Depends(get_workspace_context), db=Depends(get_db)):
    return await _get_bot(bot_id, ctx, db)


@router.patch("/{bot_id}", response_model=BotRead)
async def update_bot(
    bot_id: uuid.UUID,
    data: BotUpdate,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
):
    bot = await _get_bot(bot_id, ctx, db)
    changes = data.model_dump(exclude_unset=True)
    if bot.state in ACTIVE_STATES and ({"params", "interval", "leverage", "options"} & set(changes)):
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Pare o bot antes de alterar a configuração")
    if "params" in changes:
        changes["params"] = _validate(bot.strategy_key, changes["params"])
        bot.runtime = {}
    if "options" in changes:
        changes["options"] = data.options.model_dump() if data.options else {}
        bot.runtime = {}
    if "leverage" in changes:
        account = await get_account_in_workspace(bot.account_id, ctx, db)
        if changes["leverage"] > account.max_leverage:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Alavancagem acima do limite da conta")
    for key, value in changes.items():
        setattr(bot, key, value)
    await log_event(db, "bot.updated", user_id=ctx.user.id, workspace_id=ctx.workspace.id, bot_id=bot.id,
                    changes=changes)  # fmt: skip
    await db.commit()
    await db.refresh(bot)
    return bot


@router.delete("/{bot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_bot(
    bot_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
):
    bot = await _get_bot(bot_id, ctx, db)
    if bot.state in ACTIVE_STATES:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Pare o bot antes de excluí-lo")
    await log_event(db, "bot.deleted", user_id=ctx.user.id, workspace_id=ctx.workspace.id, name=bot.name)
    await db.delete(bot)
    await db.commit()


async def _transition(bot_id, ctx, db, action: str) -> BotConfig:
    bot = await _get_bot(bot_id, ctx, db)
    sm = BotStateMachine(bot.state)
    try:
        if action == "start":
            account = await get_account_in_workspace(bot.account_id, ctx, db)
            if account.status == AccountStatus.KILLED:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Kill switch da conta ativo")
            if await is_global_kill_switch_on(db):
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Kill switch global ativo")
            if account.execution_mode == ExecutionMode.LIVE and not settings.LIVE_TRADING_ENABLED:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Trading real desabilitado no servidor")
            if account.execution_mode == ExecutionMode.LIVE:
                require_live_mfa(ctx.user)
            if not bot.is_enabled:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Bot desabilitado")
            bot.state = sm.start()
            bot.last_error = None
            if account.status != AccountStatus.RUNNING:
                account.status = AccountStatus.RUNNING
                account.status_reason = None
        elif action == "pause":
            bot.state = sm.pause()
        elif action == "resume":
            bot.state = sm.resume()
        elif action == "stop":
            bot.state = sm.stop()
        elif action == "reset":
            if bot.state in ACTIVE_STATES:
                raise HTTPException(status.HTTP_409_CONFLICT, detail="Pare o bot antes de resetar o estado")
            bot.runtime = {}
    except InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"Transição inválida: {exc}") from exc
    await log_event(db, f"bot.{action}", user_id=ctx.user.id, workspace_id=ctx.workspace.id,
                    account_id=bot.account_id, bot_id=bot.id, state=bot.state)  # fmt: skip
    await db.commit()
    await db.refresh(bot)
    if action == "start":
        enqueue("bots.start", bot.id)
    return bot


@router.post("/{bot_id}/start", response_model=BotRead)
async def start_bot(bot_id: uuid.UUID, ctx=Depends(require_workspace_role(Role.TRADER)), db=Depends(get_db)):
    return await _transition(bot_id, ctx, db, "start")


@router.post("/{bot_id}/pause", response_model=BotRead)
async def pause_bot(bot_id: uuid.UUID, ctx=Depends(require_workspace_role(Role.TRADER)), db=Depends(get_db)):
    return await _transition(bot_id, ctx, db, "pause")


@router.post("/{bot_id}/resume", response_model=BotRead)
async def resume_bot(bot_id: uuid.UUID, ctx=Depends(require_workspace_role(Role.TRADER)), db=Depends(get_db)):
    return await _transition(bot_id, ctx, db, "resume")


@router.post("/{bot_id}/stop", response_model=BotRead)
async def stop_bot(bot_id: uuid.UUID, ctx=Depends(require_workspace_role(Role.TRADER)), db=Depends(get_db)):
    return await _transition(bot_id, ctx, db, "stop")


@router.post("/{bot_id}/reset-state", response_model=BotRead)
async def reset_bot_state(bot_id: uuid.UUID, ctx=Depends(require_workspace_role(Role.TRADER)), db=Depends(get_db)):
    """Limpa o estado interno da estratégia (ex.: grade interrompida pelo stop)."""
    return await _transition(bot_id, ctx, db, "reset")
