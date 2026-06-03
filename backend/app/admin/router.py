"""Admin API ルーター集約。

管理者専用のサブルーター (sources / pipeline / pipeline_health) を
/api/v1/admin/* 配下に集約する。ルーターレベルの get_admin_user 依存が
このパッケージ内の全エンドポイントで管理者認証を強制する。個別エンドポイントで
同依存を重複指定しないこと。
"""

from fastapi import APIRouter, Depends

from app.admin.pipeline.router import router as pipeline_router
from app.admin.pipeline_health.router import router as pipeline_health_router
from app.admin.sources.router import router as sources_router
from app.dependencies import get_admin_user

admin_router = APIRouter(
    prefix="/api/v1/admin",
    dependencies=[Depends(get_admin_user)],
)

admin_router.include_router(sources_router)
admin_router.include_router(pipeline_router)
admin_router.include_router(pipeline_health_router)

__all__ = ["admin_router"]
