from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_optional_user, get_session
from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryDetailList
from app.services.category import CategoryService

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


def get_category_service(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> CategoryService:
    return CategoryService(CategoryRepository(session))


@router.get("", dependencies=[Depends(get_optional_user)])
async def list_categories(
    service: Annotated[CategoryService, Depends(get_category_service)],
) -> CategoryDetailList:
    """全カテゴリをネストされたキーワードと記事件数付きで一覧取得する。

    認証は任意 (BFF プロキシヘッダがあれば検証、無ければ匿名扱い)。
    レスポンスはユーザー非依存だが、認可境界の一貫性のため検証だけ通す。
    """
    return await service.list_categories()
