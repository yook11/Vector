"""ソース別 health 観測用の管理者エンドポイント (read-only)。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.source_health.repository import SourceHealthRepository
from app.admin.source_health.schemas import SourceHealthResponse, WindowHours
from app.admin.source_health.service import SourceHealthService
from app.dependencies import get_session

# sources CRUD (admin/sources) と同じ /sources 名前空間を共有する。
# CRUD (操作) と health (観測) は別 feature だが URL prefix は揃える。
router = APIRouter(prefix="/sources", tags=["admin:source-health"])


def get_source_health_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> SourceHealthService:
    return SourceHealthService(SourceHealthRepository(session))


@router.get("/health")
async def get_source_health(
    service: Annotated[SourceHealthService, Depends(get_source_health_service)],
    # _CamelBase の alias_generator は生スカラ query param に効かないため明示 alias。
    window_hours: Annotated[WindowHours, Query(alias="windowHours")] = WindowHours.H24,
) -> SourceHealthResponse:
    """全ニュースソースの取得・分析可能化 health を返す。"""
    return await service.get_health(window_hours=int(window_hours))
