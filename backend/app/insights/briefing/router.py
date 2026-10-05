"""GET /api/v1/briefing/{categorySlug} ルーター。

最新週の briefing を 1 リクエストで返す。``keyArticles[].article`` に参照記事
(translatedTitle / source / url / keyPoints) を埋め込み、frontend で
N+1 fetch しないで済むようにする。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.db.fastapi import EntryManagedSession
from app.insights.briefing.query import BriefingQueryService
from app.insights.briefing.schemas import BriefingListResponse, BriefingResponse
from app.models.category import CATEGORY_SLUG_PATTERN

router = APIRouter(prefix="/api/v1/briefing", tags=["briefing"])


def get_briefing_query_service(
    session: EntryManagedSession,
) -> BriefingQueryService:
    return BriefingQueryService(session)


@router.get("")
async def list_briefings(
    service: Annotated[BriefingQueryService, Depends(get_briefing_query_service)],
) -> BriefingListResponse:
    """全カテゴリの「ある中で最新」briefing を newspaper 一覧用に返す。

    items は ``Category.id`` 昇順で 11 カテゴリ全部を返す。未生成カテゴリは
    ``latest=None`` で表現する (frontend で 1 行 ``灰色`` 表示)。
    """
    return await service.list_latest()


@router.get(
    "/{category_slug}",
    responses={404: {"description": "category not found"}},
)
async def get_latest_briefing(
    category_slug: Annotated[str, Path(pattern=CATEGORY_SLUG_PATTERN)],
    service: Annotated[BriefingQueryService, Depends(get_briefing_query_service)],
) -> BriefingResponse:
    """指定カテゴリの最新 briefing を返す (なければ state="empty")。"""
    return await service.get_latest(category_slug)
