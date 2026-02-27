from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies import get_session
from app.models.investment_category import (
    InvestmentCategory,
    InvestmentCategoryTranslation,
)
from app.schemas.category import CategoryListResponse, CategoryResponse

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.get("", response_model=CategoryListResponse)
async def list_categories(
    locale: str = Query("ja"),
    session: AsyncSession = Depends(get_session),
) -> CategoryListResponse:
    """List all investment categories ordered by slug."""
    stmt = (
        select(
            InvestmentCategory.id,
            InvestmentCategory.slug,
            InvestmentCategoryTranslation.name,
            InvestmentCategoryTranslation.description,
        )
        .join(
            InvestmentCategoryTranslation,
            InvestmentCategoryTranslation.category_id == InvestmentCategory.id,
        )
        .where(InvestmentCategoryTranslation.locale == locale)
        .order_by(InvestmentCategory.slug)
    )
    result = await session.execute(stmt)
    rows = result.all()

    return CategoryListResponse(
        items=[
            CategoryResponse(
                id=row.id,  # type: ignore[arg-type]
                slug=row.slug,
                name=row.name,
                description=row.description,
            )
            for row in rows
        ]
    )
