from fastapi import APIRouter, Depends

from app.api.deps import get_active_workspace

router = APIRouter()


@router.get("/trades")
async def list_trades(workspace=Depends(get_active_workspace)) -> list[dict]:
    """Placeholder: histórico de trades do workspace ativo."""
    return []


@router.get("/equity")
async def equity_curve(workspace=Depends(get_active_workspace)) -> list[dict]:
    """Placeholder: curva de patrimônio (snapshots gerados por services.equity)."""
    return []
