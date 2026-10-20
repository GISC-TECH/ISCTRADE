from fastapi import APIRouter, Depends

from app.api.deps import get_current_user
from app.strategy.strategies import AVAILABLE_STRATEGIES

router = APIRouter()


@router.get("/")
async def list_strategies(user=Depends(get_current_user)) -> list[dict]:
    """Lista as estratégias disponíveis (placeholder)."""
    return [{"key": k, "name": v} for k, v in AVAILABLE_STRATEGIES.items()]
