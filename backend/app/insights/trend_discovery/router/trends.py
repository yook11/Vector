"""GET /api/v1/trends ルーター。

設計判断 (failure_visibility):
- snapshot 不在は 200 + state="empty" の discriminated 構造で返す。
  「まだ生成されていない」は故障ではないため、ステータスコードでは表現しない
- ただし bundle JSONB の Pydantic validate に失敗した場合は捕まえずに 500
  伝播させる (生成側の不具合をエンドポイントで隠さない)
- 認証は任意 (BFF プロキシヘッダがあれば検証、無ければ匿名扱い)。
  レスポンスはユーザー非依存だが、認可境界の一貫性のため検証だけ通す
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_optional_user, get_session
from app.insights.trend_discovery.application.query import TrendsQueryService
from app.insights.trend_discovery.domain.trend import TrendsBundle
from app.insights.trend_discovery.schemas.trends import (
    TrendsResponse,
    empty_trends,
    trends_from_snapshot,
)

router = APIRouter(prefix="/api/v1/trends", tags=["trends"])


def get_trends_query_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TrendsQueryService:
    return TrendsQueryService(session)


@router.get("", dependencies=[Depends(get_optional_user)])
async def get_trends(
    service: Annotated[TrendsQueryService, Depends(get_trends_query_service)],
) -> TrendsResponse:
    """最新窓の trends snapshot を返す (なければ state="empty")。"""
    snapshot = await service.find_latest()
    if snapshot is None:
        return empty_trends()
    bundle = TrendsBundle.model_validate(snapshot.bundle)
    return trends_from_snapshot(
        bundle=bundle,
        generated_at=snapshot.generated_at,
        source_analysis_count=snapshot.source_analysis_count,
    )
