from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryDetailList
from app.services.category import CategoryService

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


def get_category_service(
    session: AsyncSession = Depends(get_session),
) -> CategoryService:
    return CategoryService(CategoryRepository(session))


@router.get("", response_model=CategoryDetailList)
async def list_categories(
    service: CategoryService = Depends(get_category_service),
) -> CategoryDetailList:
    """全カテゴリをネストされたキーワードと記事件数付きで一覧取得する。"""
    return await service.list_categories()
