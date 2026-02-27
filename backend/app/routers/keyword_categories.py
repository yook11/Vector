from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dependencies import get_session
from app.models.keyword_category import KeywordCategory, KeywordCategoryTranslation
from app.schemas.keyword_category import (
    KeywordCategoryListResponse,
    KeywordCategoryResponse,
)

router = APIRouter(prefix="/api/v1/keyword-categories", tags=["keyword-categories"])


@router.get("", response_model=KeywordCategoryListResponse)
async def list_keyword_categories(
    locale: str = Query("ja"),
    session: AsyncSession = Depends(get_session),
) -> KeywordCategoryListResponse:
    """List all keyword categories ordered by slug."""
    stmt = (
        select(
            KeywordCategory.id, KeywordCategory.slug, KeywordCategoryTranslation.name
        )
        .join(
            KeywordCategoryTranslation,
            KeywordCategoryTranslation.category_id == KeywordCategory.id,
        )
        .where(KeywordCategoryTranslation.locale == locale)
        .order_by(KeywordCategory.slug)
    )
    result = await session.execute(stmt)
    rows = result.all()

    return KeywordCategoryListResponse(
        items=[
            KeywordCategoryResponse(
                id=row.id,  # type: ignore[arg-type]
                slug=row.slug,
                name=row.name,
            )
            for row in rows
        ]
    )
