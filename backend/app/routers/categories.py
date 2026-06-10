from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session, require_bff_request
from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryDetailList
from app.services.category import CategoryService

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


def get_category_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryService:
    return CategoryService(CategoryRepository(session))


@router.get("", dependencies=[Depends(require_bff_request)])
async def list_categories(
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryDetailList:
    """全カテゴリをネストされたキーワードと記事件数付きで一覧取得する。

    レスポンスはユーザー非依存。BFF 経由証明を必須とし backend 直叩きを閉じるが、
    ログイン検証 (login gate) は BFF/Next.js が担うため user は要求しない。
    """
    return await service.list_categories()
