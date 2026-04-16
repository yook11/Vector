"""管理者用ルーターパッケージ。

管理者専用のサブルーターを /api/v1/admin/* 配下に集約する。
ルーターレベルの get_admin_user 依存がこのパッケージ内の全エンドポイントで
管理者認証を強制する。個別エンドポイントで同依存を重複指定しないこと。
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
