from fastapi import APIRouter

from app.api.v1.endpoints import accounts, admin, auth, bots, history, strategies, workspaces

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(workspaces.router, prefix="/workspaces", tags=["workspaces"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
api_router.include_router(bots.router, prefix="/bots", tags=["bots"])
api_router.include_router(history.router, prefix="/history", tags=["history"])
api_router.include_router(strategies.router, prefix="/strategies", tags=["strategies"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
