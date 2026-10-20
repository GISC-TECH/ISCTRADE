from fastapi import APIRouter, Depends

from app.core.security import Role, require_role

router = APIRouter()


@router.get("/stats")
async def admin_stats(user=Depends(require_role(Role.ADMIN))) -> dict:
    """Placeholder: métricas globais da plataforma (somente admin)."""
    return {"users": 0, "workspaces": 0, "accounts_running": 0}
