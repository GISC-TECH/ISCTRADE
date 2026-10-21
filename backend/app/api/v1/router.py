from fastapi import APIRouter

from app.api.v1.endpoints import (
    accounts,
    admin,
    alerts,
    auth,
    bots,
    history,
    monitoring,
    news,
    scanner,
    strategies,
    workspaces,
)

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
api_router.include_router(workspaces.router, prefix="/workspaces", tags=["workspaces"])
api_router.include_router(accounts.router, prefix="/accounts", tags=["accounts"])
api_router.include_router(bots.router, prefix="/bots", tags=["bots"])
api_router.include_router(history.router, prefix="/history", tags=["history"])
api_router.include_router(strategies.router, prefix="/strategies", tags=["strategies"])
api_router.include_router(admin.router, prefix="/admin", tags=["admin"])
api_router.include_router(alerts.router, prefix="/alerts", tags=["alerts"])
api_router.include_router(monitoring.router, prefix="/monitoring", tags=["monitoring"])
api_router.include_router(scanner.router, prefix="/scanner", tags=["scanner"])
api_router.include_router(news.router, prefix="/news", tags=["news"])
api_router.include_router(news.calendar_router, prefix="/calendar", tags=["calendar"])
