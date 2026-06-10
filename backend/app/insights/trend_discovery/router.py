"""GET /api/v1/trends ルーター。

設計判断:
- snapshot.bundle は生成時に検証済みの camelCase payload だが、read 時にも現行
  ``Trends`` schema で再検証してから返す。スキーマ進化を跨いだ旧 shape の行は
  必須フィールド欠落で ``ValidationError`` → 500 として表面化させる
  (verbatim 配信だと旧 shape が frontend を crash させるため。
  ``feedback_failure_visibility.md``)
- snapshot 不在は 200 + state="empty" の discriminated 構造で返す。
  「まだ生成されていない」は故障ではないため、ステータスコードでは表現しない
- レスポンスはユーザー非依存。BFF 経由証明を必須とし backend 直叩きを閉じるが、
  ログイン検証 (login gate) は BFF/Next.js が担うため user は要求しない
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session, require_bff_request
from app.insights.trend_discovery.query import TrendsQueryService
from app.insights.trend_discovery.schemas import (
    Trends,
    TrendsResponse,
    empty_trends,
)

router = APIRouter(prefix="/api/v1/trends", tags=["trends"])


def get_trends_query_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TrendsQueryService:
    return TrendsQueryService(session)


@router.get(
    "",
    response_model=TrendsResponse,
    dependencies=[Depends(require_bff_request)],
)
async def get_trends(
    service: Annotated[TrendsQueryService, Depends(get_trends_query_service)],
) -> TrendsResponse:
    """最新窓の trends snapshot を返す (なければ state="empty")。

    保存済み bundle を ``Trends`` schema で再検証する。現行 contract に合わない
    (旧 shape 等) 場合は ``ValidationError`` が伝播し FastAPI が 500 を返す。
    """
    snapshot = await service.find_latest()
    if snapshot is None:
        return empty_trends()
    return Trends.model_validate(snapshot.bundle)
