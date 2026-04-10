"""Admin router package.

Aggregates admin-only sub-routers under /api/v1/admin/*.
Router-level get_admin_user dependency enforces admin auth for every endpoint
in this package. Individual endpoints must NOT repeat the dependency.
"""

from fastapi import APIRouter, Depends

from app.dependencies import get_admin_user
from app.routers.admin import news_sources, pipeline

admin_router = APIRouter(
    prefix="/api/v1/admin",
    dependencies=[Depends(get_admin_user)],
)

admin_router.include_router(news_sources.router)
admin_router.include_router(pipeline.router)

__all__ = ["admin_router"]
