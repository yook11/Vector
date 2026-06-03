"""news_sources 管理のための CRUD エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin.sources.repository import NewsSourceRepository
from app.admin.sources.schemas import (
    NewsSourceCreate,
    NewsSourceDetail,
    NewsSourceDetailList,
)
from app.admin.sources.service import NewsSourceService
from app.dependencies import get_session

# news_sources.id は PostgreSQL INTEGER (int32) のため、上限を path level で
# 明示して OverflowError 由来の 500 leak を構造的に閉塞する。下限 1 は
# Schemathesis が default で生成する id=0 を 422 で弾き、404 経路 (実際の
# DB lookup miss) と区別するため。
_INT32_MAX = 2_147_483_647
_SourceId = Annotated[int, Path(ge=1, le=_INT32_MAX)]

router = APIRouter(prefix="/sources", tags=["admin:sources"])


def get_news_source_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> NewsSourceService:
    return NewsSourceService(NewsSourceRepository(session))


@router.get("")
async def list_news_sources(
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetailList:
    """全ニュースソースを一覧取得する。"""
    return await service.get_all()


@router.post("", status_code=201)
async def create_news_source(
    body: NewsSourceCreate,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetail:
    """新しいニュースソースを作成する。"""
    return await service.create_source(body)


@router.delete(
    "/{source_id}",
    status_code=204,
    responses={404: {"description": "News source not found"}},
)
async def delete_news_source(
    source_id: _SourceId,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> None:
    """ニュースソースを削除する。"""
    await service.delete_source(source_id)


@router.patch(
    "/{source_id}/activate",
    responses={404: {"description": "News source not found"}},
)
async def activate_source(
    source_id: _SourceId,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetail:
    """ニュースソースを有効化する。"""
    return await service.activate_source(source_id)


@router.patch(
    "/{source_id}/deactivate",
    responses={404: {"description": "News source not found"}},
)
async def deactivate_source(
    source_id: _SourceId,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetail:
    """ニュースソースを無効化する。"""
    return await service.deactivate_source(source_id)
