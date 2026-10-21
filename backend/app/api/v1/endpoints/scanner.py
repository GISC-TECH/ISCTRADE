"""Market scanner (latest snapshot, filters and on-demand refresh)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import WorkspaceContext, get_workspace_context, require_workspace_role
from app.core.config import settings
from app.core.database import get_db
from app.core.rate_limiting import rate_limiter
from app.core.security import Role
from app.services.dispatch import enqueue
from app.services.scanner import DEFAULT_WEIGHTS, SIGNAL_STRATEGIES, filter_results, latest_scan

router = APIRouter()


class ScannerResponse(BaseModel):
    scanned_at: datetime | None
    interval: str
    testnet: bool
    symbols_scanned: int
    total: int
    duration_ms: int
    weights: dict[str, float]
    strategies: list[str]
    results: list[dict[str, Any]]


@router.get("/", response_model=ScannerResponse)
async def get_scanner(
    _: WorkspaceContext = Depends(get_workspace_context),
    db: AsyncSession = Depends(get_db),
    sort: str = "score",
    order: Literal["asc", "desc"] = "desc",
    min_score: float | None = Query(None, ge=0, le=100),
    min_quote_volume: float | None = Query(None, ge=0),
    trend: Literal["up", "down", "flat"] | None = None,
    bias: Literal["long", "short", "neutral"] | None = None,
    signal: str | None = None,
    search: str | None = Query(None, max_length=20),
    limit: int = Query(100, ge=1, le=500),
) -> ScannerResponse:
    """Último snapshot do scanner. Informação de mercado, não recomendação de investimento."""
    scan = await latest_scan(db)
    if scan is None:
        return ScannerResponse(
            scanned_at=None,
            interval=settings.SCANNER_INTERVAL,
            testnet=settings.SCANNER_USE_TESTNET,
            symbols_scanned=0,
            total=0,
            duration_ms=0,
            weights=DEFAULT_WEIGHTS,
            strategies=list(SIGNAL_STRATEGIES),
            results=[],
        )
    rows = filter_results(
        scan.results,
        min_quote_volume=min_quote_volume,
        min_score=min_score,
        trend=trend,
        bias=bias,
        signal=signal,
        search=search,
        sort=sort,
        desc=order == "desc",
    )
    return ScannerResponse(
        scanned_at=scan.created_at,
        interval=scan.interval,
        testnet=scan.testnet,
        symbols_scanned=scan.symbols_scanned,
        total=len(rows),
        duration_ms=scan.duration_ms,
        weights=(scan.params or {}).get("weights", DEFAULT_WEIGHTS),
        strategies=(scan.params or {}).get("strategies", list(SIGNAL_STRATEGIES)),
        results=rows[:limit],
    )


@router.post("/refresh", status_code=202, dependencies=[Depends(rate_limiter(3, scope="scanner-refresh"))])
async def refresh_scanner(_: WorkspaceContext = Depends(require_workspace_role(Role.TRADER))) -> dict[str, bool]:
    """Agenda uma nova varredura (worker Celery)."""
    return {"queued": enqueue("scanner.run")}
