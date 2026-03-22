from collections import defaultdict

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.dependencies import get_session
from app.models.article_group import ArticleGroup
from app.models.associations import NewsKeyword
from app.models.keyword import Keyword
from app.models.keyword_category import (
    KeywordCategory,
    KeywordCategoryLink,
    KeywordCategoryTranslation,
)
from app.models.news import NewsArticle
from app.schemas.keyword_category import (
    KeywordCategoryDetailListResponse,
    KeywordCategoryDetailResponse,
    KeywordInCategory,
)

router = APIRouter(prefix="/api/v1/keyword-categories", tags=["keyword-categories"])


@router.get("", response_model=KeywordCategoryDetailListResponse)
async def list_keyword_categories(
    locale: str = Query("ja"),
    session: AsyncSession = Depends(get_session),
) -> KeywordCategoryDetailListResponse:
    """List all keyword categories with nested keywords and article counts."""

    # 0. Subquery: visible article IDs after deduplication
    canonical_ids = select(ArticleGroup.canonical_id).where(
        ArticleGroup.canonical_id.is_not(None)
    )
    visible_article_ids = select(NewsArticle.id).where(
        (NewsArticle.article_group_id.is_(None)) | (NewsArticle.id.in_(canonical_ids))
    )

    # 1. Fetch categories with translated names
    cat_stmt = (
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
    cat_result = await session.execute(cat_stmt)
    cat_rows = cat_result.all()

    # 2. Fetch per-keyword article counts grouped by category
    kw_stmt = (
        select(
            KeywordCategoryLink.category_id,
            Keyword.id.label("keyword_id"),
            Keyword.keyword,
            func.count(func.distinct(NewsKeyword.news_article_id)).label(
                "article_count"
            ),
        )
        .join(Keyword, Keyword.id == KeywordCategoryLink.keyword_id)
        .outerjoin(
            NewsKeyword,
            (NewsKeyword.keyword_id == Keyword.id)
            & (NewsKeyword.news_article_id.in_(visible_article_ids)),
        )
        .group_by(KeywordCategoryLink.category_id, Keyword.id, Keyword.keyword)
        .order_by(Keyword.keyword)
    )
    kw_result = await session.execute(kw_stmt)
    kw_rows = kw_result.all()

    # 3. Fetch per-category distinct article counts
    cat_count_stmt = (
        select(
            KeywordCategoryLink.category_id,
            func.count(func.distinct(NewsKeyword.news_article_id)).label(
                "article_count"
            ),
        )
        .join(NewsKeyword, NewsKeyword.keyword_id == KeywordCategoryLink.keyword_id)
        .where(NewsKeyword.news_article_id.in_(visible_article_ids))
        .group_by(KeywordCategoryLink.category_id)
    )
    cat_count_result = await session.execute(cat_count_stmt)
    cat_counts: dict[int, int] = {
        row.category_id: row.article_count for row in cat_count_result.all()
    }

    # 4. Group keywords by category_id
    kw_by_cat: dict[int, list[KeywordInCategory]] = defaultdict(list)
    for row in kw_rows:
        kw_by_cat[row.category_id].append(
            KeywordInCategory(
                id=row.keyword_id,
                keyword=row.keyword,
                article_count=row.article_count,
            )
        )

    # 5. Build response
    return KeywordCategoryDetailListResponse(
        items=[
            KeywordCategoryDetailResponse(
                id=row.id,  # type: ignore[arg-type]
                slug=row.slug,
                name=row.name,
                article_count=cat_counts.get(row.id, 0),  # type: ignore[arg-type]
                keywords=kw_by_cat.get(row.id, []),  # type: ignore[arg-type]
            )
            for row in cat_rows
        ]
    )
