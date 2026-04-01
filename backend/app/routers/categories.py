from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_session
from app.repositories.category import CategoryRepository
from app.schemas.category import CategoryDetailList
from app.services.category import CategoryService

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.get("", response_model=CategoryDetailList)
async def list_categories(
    session: AsyncSession = Depends(get_session),
) -> CategoryDetailList:
    """List all categories with nested keywords and article counts."""
    repo = CategoryRepository(session)
    service = CategoryService(repo)
    return await service.list_categories()
