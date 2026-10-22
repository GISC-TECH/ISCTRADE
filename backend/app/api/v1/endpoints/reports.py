"""PnL by period and CSV exports (trades/income) for accounting. Mounted under /history."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context
from app.core.database import get_db
from app.core.rate_limiting import client_ip, rate_limiter
from app.services import pnl_report
from app.services.audit import log_event

router = APIRouter()


class PnlTotals(BaseModel):
    realized_pnl: Decimal
    fees: Decimal
    net_pnl: Decimal
    trades: int
    fees_other: dict[str, Decimal]


class PnlBucket(PnlTotals):
    period_start: str
    group: str
    label: str


class PnlGroup(PnlTotals):
    group: str
    label: str


class PnlReport(BaseModel):
    period: str
    group_by: str
    start: datetime
    end: datetime
    buckets: list[PnlBucket]
    groups: list[PnlGroup]
    totals: PnlTotals


def _range(period: pnl_report.Period, start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    try:
        return pnl_report.resolve_range(period, start, end)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/pnl", response_model=PnlReport)
async def pnl(
    period: pnl_report.Period = "day",
    group_by: pnl_report.GroupBy = "none",
    account_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    """PnL realizado por dia/semana/mês (UTC), opcionalmente por conta, bot, estratégia ou símbolo.

    Padrão: últimos 30 dias (dia), 26 semanas (semana) ou 12 meses (mês). Taxas em USDT/USDC entram em `fees`;
    taxas pagas em outros ativos (ex.: BNB) ficam em `fees_other`."""
    s, e = _range(period, start, end)
    report = await pnl_report.pnl_by_period(db, ctx.workspace.id, period, group_by, s, e, account_id)
    for b in report["buckets"]:
        b["period_start"] = b["period_start"].isoformat()
    return report


def _csv_response(gen, filename: str) -> StreamingResponse:
    return StreamingResponse(
        gen,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"},
    )


_export_limit = Depends(rate_limiter(10, scope="history-export"))


@router.get("/export/trades.csv", dependencies=[_export_limit])
async def export_trades(
    request: Request,
    account_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    delimiter: Literal["comma", "semicolon"] = Query("comma"),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    """Execuções (fills) em CSV para contabilidade: UTC, valores com ponto decimal (padrão: últimos 12 meses)."""
    s, e = _range("month", start, end)
    await log_event(db, "history.export", user_id=ctx.user.id, workspace_id=ctx.workspace.id,
                    ip_address=client_ip(request), kind="trades", start=s.isoformat(), end=e.isoformat(),
                    commit=True)  # fmt: skip
    gen = pnl_report.trades_csv(db, ctx.workspace.id, s, e, account_id, ";" if delimiter == "semicolon" else ",")
    return _csv_response(gen, f"isctrade-trades-{s:%Y%m%d}-{e:%Y%m%d}.csv")


@router.get("/export/income.csv", dependencies=[_export_limit])
async def export_income(
    request: Request,
    period: pnl_report.Period = "day",
    account_id: uuid.UUID | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    delimiter: Literal["comma", "semicolon"] = Query("comma"),
    ctx: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
):
    """PnL realizado e taxas por período e conta em CSV (padrão: últimos 12 meses)."""
    s, e = _range("month", start, end)
    await log_event(db, "history.export", user_id=ctx.user.id, workspace_id=ctx.workspace.id,
                    ip_address=client_ip(request), kind="income", period=period, start=s.isoformat(),
                    end=e.isoformat(), commit=True)  # fmt: skip
    sep = ";" if delimiter == "semicolon" else ","
    gen = pnl_report.income_csv(db, ctx.workspace.id, period, s, e, account_id, sep)
    return _csv_response(gen, f"isctrade-pnl-{period}-{s:%Y%m%d}-{e:%Y%m%d}.csv")
