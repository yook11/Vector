from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.dependencies import get_session
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.schemas.category import (
    CategoryDetail,
    CategoryDetailList,
)
from app.schemas.embeds import KeywordStatEmbed

router = APIRouter(prefix="/api/v1/categories", tags=["categories"])


@router.get("", response_model=CategoryDetailList)
async def list_categories(
    session: AsyncSession = Depends(get_session),
) -> CategoryDetailList:
    """List all categories with nested keywords and article counts."""

    # 1. Fetch categories (name is a direct column)
    cat_stmt = select(Category.id, Category.slug, Category.name).order_by(Category.slug)
    cat_result = await session.execute(cat_stmt)
    cat_rows = cat_result.all()

    # 2. Fetch per-keyword article counts grouped by category (1:N via category_id)
    kw_stmt = (
        select(
            Keyword.category_id,
            Keyword.id.label("keyword_id"),
            Keyword.name,
            func.count(func.distinct(ArticleKeyword.news_article_id)).label(
                "article_count"
            ),
        )
        .outerjoin(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
        .group_by(Keyword.category_id, Keyword.id, Keyword.name)
        .order_by(Keyword.name)
    )
    kw_result = await session.execute(kw_stmt)
    kw_rows = kw_result.all()

    # 3. Fetch per-category distinct article counts
    cat_count_stmt = (
        select(
            Keyword.category_id,
            func.count(func.distinct(ArticleKeyword.news_article_id)).label(
                "article_count"
            ),
        )
        .join(ArticleKeyword, ArticleKeyword.keyword_id == Keyword.id)
        .group_by(Keyword.category_id)
    )
    cat_count_result = await session.execute(cat_count_stmt)
    cat_counts: dict[int, int] = {
        row.category_id: row.article_count for row in cat_count_result.all()
    }

    # 4. Group keywords by category_id
    kw_by_cat: dict[int, list[KeywordStatEmbed]] = defaultdict(list)
    for row in kw_rows:
        kw_by_cat[row.category_id].append(
            KeywordStatEmbed(
                id=row.keyword_id,
                name=row.name,
                article_count=row.article_count,
            )
        )

    # 5. Build response
    return CategoryDetailList(
        items=[
            CategoryDetail(
                id=row.id,  # type: ignore[arg-type]
                slug=row.slug,
                name=row.name,
                article_count=cat_counts.get(row.id, 0),  # type: ignore[arg-type]
                keywords=kw_by_cat.get(row.id, []),  # type: ignore[arg-type]
            )
            for row in cat_rows
        ]
    )
