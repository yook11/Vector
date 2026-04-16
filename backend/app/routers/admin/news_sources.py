"""news_sources 管理のための CRUD エンドポイント。"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.repositories.news_source import NewsSourceRepository
from app.schemas.news_source import (
    NewsSourceCreate,
    NewsSourceDetail,
    NewsSourceDetailList,
)
from app.services.news_source import NewsSourceService

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


@router.delete("/{source_id}", status_code=204)
async def delete_news_source(
    source_id: int,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> None:
    """ニュースソースを削除する。"""
    await service.delete_source(source_id)


@router.patch("/{source_id}/activate")
async def activate_source(
    source_id: int,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetail:
    """ニュースソースを有効化する。"""
    return await service.activate_source(source_id)


@router.patch("/{source_id}/deactivate")
async def deactivate_source(
    source_id: int,
    service: Annotated[NewsSourceService, Depends(get_news_source_service)],
) -> NewsSourceDetail:
    """ニュースソースを無効化する。"""
    return await service.deactivate_source(source_id)
