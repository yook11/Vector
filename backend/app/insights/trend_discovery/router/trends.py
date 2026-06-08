"""GET /api/v1/trends ルーター。

設計判断:
- snapshot.bundle は生成時に検証済みの API payload (camelCase) なので、read は
  そのまま (verbatim) 返す。読取時の validate・変換は持たない (検証は生成時 1 回)
- snapshot 不在は 200 + state="empty" の discriminated 構造で返す。
  「まだ生成されていない」は故障ではないため、ステータスコードでは表現しない
- 認証は任意 (BFF プロキシヘッダがあれば検証、無ければ匿名扱い)。
  レスポンスはユーザー非依存だが、認可境界の一貫性のため検証だけ通す
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_optional_user, get_session
from app.insights.trend_discovery.application.query import TrendsQueryService
from app.insights.trend_discovery.schemas.trends import TrendsResponse, empty_trends

router = APIRouter(prefix="/api/v1/trends", tags=["trends"])

# snapshot 不在時の固定 payload。schema 起点で組み、表記揺れを防ぐ。
_EMPTY_TRENDS = empty_trends().model_dump(mode="json", by_alias=True)


def get_trends_query_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> TrendsQueryService:
    return TrendsQueryService(session)


@router.get(
    "",
    response_model=TrendsResponse,
    dependencies=[Depends(get_optional_user)],
)
async def get_trends(
    service: Annotated[TrendsQueryService, Depends(get_trends_query_service)],
) -> Response:
    """最新窓の trends snapshot を verbatim で返す (なければ state="empty")。

    ``response_model`` は OpenAPI/型生成のためだけに宣言する。``Response`` を直接
    返すため FastAPI は実体の再検証・再シリアライズをしない (保存済み payload を
    そのまま配信する)。
    """
    snapshot = await service.find_latest()
    if snapshot is None:
        return JSONResponse(content=_EMPTY_TRENDS)
    return JSONResponse(content=snapshot.bundle)
