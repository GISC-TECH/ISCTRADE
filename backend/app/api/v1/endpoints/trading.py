"""Manual trading (real-time panel). TRADER role; every opening order goes through the RiskManager."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, require_workspace_role
from app.api.v1.endpoints.accounts import get_account_in_workspace
from app.core.database import get_db
from app.core.security import Role
from app.models import OrderType
from app.schemas.history import OrderRead
from app.schemas.trading import (
    CloseAllRequest,
    CloseAllResult,
    ClosePositionRequest,
    LeverageRequest,
    LeverageResult,
    ManualOrderCreate,
    ManualOrderResult,
)
from app.services import manual_trading
from app.services.execution import ExecutionResult
from app.services.manual_trading import ManualOrderRequest, ManualTradingError

router = APIRouter()


def _confirm(flag: bool) -> None:
    if not flag:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Confirmação obrigatória (confirm=true)")


def _result(res: ExecutionResult | None, mode: str) -> ManualOrderResult:
    if res is None:
        return ManualOrderResult(ok=True, status="no_position", reason="no_position", mode=mode)
    return ManualOrderResult(
        ok=res.ok,
        status=res.order.status.value if res.order is not None else res.reason,
        reason=res.reason,
        mode=mode,
        order=OrderRead.model_validate(res.order) if res.order is not None and res.order.id else None,
        fill_price=res.fill_price,
        quantity=res.quantity,
        realized_pnl=res.realized_pnl,
    )


async def _run(db: AsyncSession, coro):
    try:
        out = await coro
    except ManualTradingError as exc:
        await db.commit()  # keep the audit trail of blocked actions
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    await db.commit()
    return out


@router.post("/orders", response_model=ManualOrderResult)
async def place_order(
    data: ManualOrderCreate,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> ManualOrderResult:
    """Ordem manual MARKET/LIMIT com stop loss obrigatório e take profit opcional."""
    _confirm(data.confirm)
    account = await get_account_in_workspace(data.account_id, ctx, db)
    req = ManualOrderRequest(
        symbol=data.symbol, side=data.side, type=OrderType(data.type), quantity=data.quantity,
        stop_loss=data.stop_loss, price=data.price, take_profit=data.take_profit, leverage=data.leverage,
    )  # fmt: skip
    res = await _run(db, manual_trading.place_order(db, account, req, ctx.user.id))
    if res.order is not None:
        await db.refresh(res.order)
    return _result(res, account.execution_mode.value)


@router.delete("/orders/{order_id}", response_model=OrderRead)
async def cancel_order(
    order_id: uuid.UUID,
    account_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
):
    account = await get_account_in_workspace(account_id, ctx, db)
    order = await _run(db, manual_trading.cancel_order(db, account, order_id, ctx.user.id))
    await db.refresh(order)
    return order


@router.post("/close", response_model=ManualOrderResult)
async def close_position(
    data: ClosePositionRequest,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> ManualOrderResult:
    """Fecha a posição inteira (reduce-only) e cancela ordens abertas do símbolo. Permitido sob kill switch."""
    _confirm(data.confirm)
    account = await get_account_in_workspace(data.account_id, ctx, db)
    res = await _run(db, manual_trading.close_position(db, account, data.symbol, ctx.user.id))
    if res is not None and res.order is not None:
        await db.refresh(res.order)
    return _result(res, account.execution_mode.value)


@router.post("/close-all", response_model=CloseAllResult)
async def close_all(
    data: CloseAllRequest,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
):
    _confirm(data.confirm)
    account = await get_account_in_workspace(data.account_id, ctx, db)
    return await _run(db, manual_trading.close_all(db, account, ctx.user.id))


@router.post("/leverage", response_model=LeverageResult)
async def set_leverage(
    data: LeverageRequest,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
):
    _confirm(data.confirm)
    account = await get_account_in_workspace(data.account_id, ctx, db)
    lev = await _run(db, manual_trading.set_leverage(db, account, data.symbol, data.leverage, ctx.user.id))
    return {"symbol": data.symbol, "leverage": lev}
