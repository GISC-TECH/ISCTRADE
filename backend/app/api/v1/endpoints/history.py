import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import Pagination, WorkspaceContext, get_workspace_context, pagination, require_workspace_role
from app.core.database import get_db
from app.core.security import Role
from app.models import AuditLog, EquitySnapshot, Order, OrderStatus, Position, Trade
from app.schemas.common import Page
from app.schemas.history import AuditRead, EquityPointRead, OrderRead, PositionRead, TradeRead

router = APIRouter()


async def paginate(db: AsyncSession, stmt: Select, page: Pagination, order_by) -> dict:
    total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    items = list(await db.scalars(stmt.order_by(order_by).limit(page.limit).offset(page.offset)))
    return {"items": items, "total": total or 0, "limit": page.limit, "offset": page.offset}


@router.get("/orders", response_model=Page[OrderRead])
async def list_orders(
    account_id: uuid.UUID | None = None,
    bot_id: uuid.UUID | None = None,
    symbol: str | None = None,
    status: OrderStatus | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Pagination = Depends(pagination),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Order).where(Order.workspace_id == ctx.workspace.id)
    if account_id:
        stmt = stmt.where(Order.account_id == account_id)
    if bot_id:
        stmt = stmt.where(Order.bot_id == bot_id)
    if symbol:
        stmt = stmt.where(Order.symbol == symbol.upper())
    if status:
        stmt = stmt.where(Order.status == status)
    if start:
        stmt = stmt.where(Order.created_at >= start)
    if end:
        stmt = stmt.where(Order.created_at <= end)
    return await paginate(db, stmt, page, Order.created_at.desc())


@router.get("/trades", response_model=Page[TradeRead])
async def list_trades(
    account_id: uuid.UUID | None = None,
    bot_id: uuid.UUID | None = None,
    symbol: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Pagination = Depends(pagination),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Trade).where(Trade.workspace_id == ctx.workspace.id)
    if account_id:
        stmt = stmt.where(Trade.account_id == account_id)
    if bot_id:
        stmt = stmt.where(Trade.bot_id == bot_id)
    if symbol:
        stmt = stmt.where(Trade.symbol == symbol.upper())
    if start:
        stmt = stmt.where(Trade.executed_at >= start)
    if end:
        stmt = stmt.where(Trade.executed_at <= end)
    return await paginate(db, stmt, page, Trade.executed_at.desc())


@router.get("/equity", response_model=list[EquityPointRead])
async def equity_curve(
    account_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = Query(1000, ge=1, le=10000),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    """Snapshots de patrimônio (mais recentes primeiro são cortados pelo limite; retorno em ordem cronológica)."""
    stmt = select(EquitySnapshot).where(EquitySnapshot.workspace_id == ctx.workspace.id)
    if account_id:
        stmt = stmt.where(EquitySnapshot.account_id == account_id)
    if start:
        stmt = stmt.where(EquitySnapshot.taken_at >= start)
    if end:
        stmt = stmt.where(EquitySnapshot.taken_at <= end)
    rows = list(await db.scalars(stmt.order_by(EquitySnapshot.taken_at.desc()).limit(limit)))
    return list(reversed(rows))


@router.get("/positions", response_model=list[PositionRead])
async def list_positions(
    account_id: uuid.UUID | None = None,
    include_closed: bool = False,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Position).where(Position.workspace_id == ctx.workspace.id)
    if account_id:
        stmt = stmt.where(Position.account_id == account_id)
    if not include_closed:
        stmt = stmt.where(Position.quantity != 0)
    return list(await db.scalars(stmt.order_by(Position.symbol)))


@router.get("/audit", response_model=Page[AuditRead])
async def list_audit(
    event: str | None = None,
    level: str | None = None,
    account_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    page: Pagination = Depends(pagination),
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AuditLog).where(AuditLog.workspace_id == ctx.workspace.id)
    if event:
        stmt = stmt.where(AuditLog.event.startswith(event))
    if level:
        stmt = stmt.where(AuditLog.level == level)
    if account_id:
        stmt = stmt.where(AuditLog.account_id == account_id)
    if start:
        stmt = stmt.where(AuditLog.created_at >= start)
    if end:
        stmt = stmt.where(AuditLog.created_at <= end)
    return await paginate(db, stmt, page, AuditLog.created_at.desc())
