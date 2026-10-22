"""Aggregate risk limits (G-02): per workspace and instance-wide. Loosening is restricted to admins."""

import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context, require_superadmin, require_workspace_role
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiting import client_ip
from app.core.security import Role
from app.models import User, Workspace
from app.schemas.risk import RiskLimitsIn, RiskLimitsOut, RiskLimitsOverview
from app.services.audit import log_event
from app.services.risk_limits import (
    FIELDS,
    AggregateLimits,
    default_workspace_limits,
    get_global_limits,
    get_workspace_limits,
    instance_capital_cap,
    set_global_limits,
    set_workspace_limits,
)

router = APIRouter()


def _out(limits: AggregateLimits) -> RiskLimitsOut:
    return RiskLimitsOut(**asdict(limits))


def _merge(current: AggregateLimits, data: RiskLimitsIn) -> AggregateLimits:
    changes = data.model_dump(exclude_unset=True)
    return AggregateLimits.build(*(changes[k] if k in changes else getattr(current, k) for k in FIELDS))


@router.get("/limits", response_model=RiskLimitsOverview)
async def get_limits(
    ctx: WorkspaceContext = Depends(get_workspace_context), db: AsyncSession = Depends(get_db)
) -> RiskLimitsOverview:
    """Limites agregados efetivos do workspace ativo e da instância."""
    return RiskLimitsOverview(
        workspace=_out(await get_workspace_limits(db, ctx.workspace.id)),
        workspace_defaults=_out(default_workspace_limits()),
        global_limits=_out(await get_global_limits(db)),
        live_account_max_capital_usdt=instance_capital_cap(),
        max_leverage_cap=settings.RISK_MAX_LEVERAGE_CAP,
        include_paper=settings.RISK_AGGREGATE_INCLUDE_PAPER,
    )


@router.put("/limits", response_model=RiskLimitsOut)
async def update_limits(
    data: RiskLimitsIn,
    request: Request,
    ctx: WorkspaceContext = Depends(require_workspace_role(Role.TRADER)),
    db: AsyncSession = Depends(get_db),
) -> RiskLimitsOut:
    """Trader: só aperta. Admin do workspace: até os padrões do servidor. Acima disso: só o super-admin."""
    current = await get_workspace_limits(db, ctx.workspace.id)
    new = _merge(current, data)
    if new.loosened_fields(current):
        ctx.require(Role.ADMIN)
    above_defaults = new.loosened_fields(default_workspace_limits())
    if above_defaults and ctx.user.role != Role.ADMIN:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            detail="Somente o super-admin da plataforma pode afrouxar além dos padrões do servidor"
            " (PUT /risk/workspaces/{id}/limits): " + ", ".join(above_defaults),
        )
    await set_workspace_limits(db, ctx.workspace.id, new)
    await log_event(
        db, "risk.limits_updated", level="warning", user_id=ctx.user.id, workspace_id=ctx.workspace.id,
        ip_address=client_ip(request), scope="workspace", before=current.to_json(), after=new.to_json(),
    )  # fmt: skip
    await db.commit()
    return _out(new)


@router.put("/workspaces/{workspace_id}/limits", response_model=RiskLimitsOut)
async def admin_update_workspace_limits(
    workspace_id: uuid.UUID,
    data: RiskLimitsIn,
    request: Request,
    admin: User = Depends(require_superadmin),
    db: AsyncSession = Depends(get_db),
) -> RiskLimitsOut:
    """Super-admin: define os limites de qualquer workspace, inclusive acima dos padrões do servidor."""
    if await db.get(Workspace, workspace_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Workspace não encontrado")
    current = await get_workspace_limits(db, workspace_id)
    new = _merge(current, data)
    await set_workspace_limits(db, workspace_id, new)
    await log_event(
        db, "risk.limits_updated", level="warning", user_id=admin.id, workspace_id=workspace_id,
        ip_address=client_ip(request), scope="workspace", before=current.to_json(), after=new.to_json(),
    )  # fmt: skip
    await db.commit()
    return _out(new)


@router.get("/global-limits", response_model=RiskLimitsOut)
async def get_global(_: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)) -> RiskLimitsOut:
    return _out(await get_global_limits(db))


@router.put("/global-limits", response_model=RiskLimitsOut)
async def update_global(
    data: RiskLimitsIn, request: Request, admin: User = Depends(require_superadmin), db: AsyncSession = Depends(get_db)
) -> RiskLimitsOut:
    """Limites somados de todos os workspaces (somente super-admin)."""
    current = await get_global_limits(db)
    new = _merge(current, data)
    await set_global_limits(db, new)
    await log_event(
        db, "risk.limits_updated", level="warning", user_id=admin.id, ip_address=client_ip(request),
        scope="global", before=current.to_json(), after=new.to_json(),
    )  # fmt: skip
    await db.commit()
    return _out(new)
