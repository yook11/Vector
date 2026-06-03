"""pipeline health 観測用の管理者エンドポイント (read-only)。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.pipeline_health.repository import PipelineHealthRepository
from app.admin.pipeline_health.schemas import PipelineHealthResponse
from app.admin.pipeline_health.service import PipelineHealthService
from app.dependencies import get_session

# pipeline fetch (admin/pipeline) と同じ /pipeline 名前空間を共有する。
# fetch (操作) と health (観測) は別 feature だが URL prefix は揃える。
router = APIRouter(prefix="/pipeline", tags=["admin:pipeline-health"])


def get_pipeline_health_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> PipelineHealthService:
    return PipelineHealthService(PipelineHealthRepository(session))


@router.get("/health")
async def get_pipeline_health(
    service: Annotated[PipelineHealthService, Depends(get_pipeline_health_service)],
) -> PipelineHealthResponse:
    """パイプライン各 stage の健全性スナップショットを返す。"""
    return await service.get_health()
