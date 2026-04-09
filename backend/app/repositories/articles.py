"""Read-only queries for articles (listing, detail, similar)."""

from sqlalchemy import exists, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import contains_eager, defer, selectinload

from app.models.article_analysis import ArticleAnalysis
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.schemas.articles import ArticleListParams, SortOrder


def article_eager_options_brief() -> list:
    """一覧用. 呼び出し側で .join(ArticleAnalysis.news_article) が必要."""
    return [
        contains_eager(ArticleAnalysis.news_article).options(
            defer(NewsArticle.original_content, raiseload=True),
            selectinload(NewsArticle.news_source),
        ),
        selectinload(ArticleAnalysis.article_keywords).selectinload(
            ArticleKeyword.keyword
        ),
    ]


def article_eager_options_detail() -> list:
    """詳細用. 呼び出し側で .join(ArticleAnalysis.news_article) が必要."""
    return [
        contains_eager(ArticleAnalysis.news_article).options(
            selectinload(NewsArticle.news_source),
        ),
        selectinload(ArticleAnalysis.article_keywords).selectinload(
            ArticleKeyword.keyword
        ),
    ]


class ArticleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    # -- public: listing ------------------------------------------------

    async def fetch_articles(
        self,
        query: ArticleListParams,
    ) -> tuple[list[ArticleAnalysis], int]:
        """Fetch paginated article list for news browsing."""
        stmt = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.news_article)
            .options(*article_eager_options_brief())
        )

        # Filters
        if query.keyword is not None:
            matching_ids = (
                select(ArticleKeyword.article_analysis_id)
                .join(Keyword, Keyword.id == ArticleKeyword.keyword_id)
                .where(Keyword.name == query.keyword)
            )
            stmt = stmt.where(ArticleAnalysis.id.in_(matching_ids))
        elif query.category is not None:
            cat_id_sub = select(Category.id).where(Category.slug == query.category)
            sub_kw_ids = select(Keyword.id).where(Keyword.category_id.in_(cat_id_sub))
            matching_ids = select(ArticleKeyword.article_analysis_id).where(
                ArticleKeyword.keyword_id.in_(sub_kw_ids)
            )
            stmt = stmt.where(ArticleAnalysis.id.in_(matching_ids))

        if query.impact_level is not None:
            stmt = stmt.where(ArticleAnalysis.impact_level == query.impact_level)

        # Count
        count_stmt = select(func.count()).select_from(stmt.subquery())
        total = (await self.session.execute(count_stmt)).scalar_one()

        # Sort
        order = (
            NewsArticle.published_at.desc()
            if query.sort_order == SortOrder.DESC
            else NewsArticle.published_at.asc()
        )
        stmt = stmt.order_by(order, ArticleAnalysis.id.desc())

        # Paginate
        stmt = stmt.offset(query.offset).limit(query.limit)

        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all()), total

    async def fetch_one_analyzed(self, article_id: int) -> ArticleAnalysis | None:
        """Fetch a single article with analysis eager-loaded.

        Returns None if not found or not analyzed.
        """
        stmt = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.news_article)
            .where(ArticleAnalysis.id == article_id)
            .options(*article_eager_options_detail())
        )
        result = await self.session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def exists_analyzed(self, article_id: int) -> bool:
        """Check whether an analyzed article exists."""
        stmt = select(exists().where(ArticleAnalysis.id == article_id))
        result = await self.session.execute(stmt)
        return result.scalar_one()

    async def fetch_similar_to(
        self, article_id: int, limit: int
    ) -> list[ArticleAnalysis]:
        """Fetch articles similar to the given article, ordered by cosine distance.

        Returns an empty list when the article does not exist or has no embedding.
        """
        source_embedding = (
            select(ArticleAnalysis.embedding)
            .where(
                ArticleAnalysis.id == article_id,
                ArticleAnalysis.embedding.is_not(None),
            )
            .cte("source_embedding")
        )

        stmt = (
            select(ArticleAnalysis)
            .join(ArticleAnalysis.news_article)
            .join(source_embedding, true())
            .options(*article_eager_options_brief())
            .where(
                ArticleAnalysis.id != article_id,
                ArticleAnalysis.embedding.is_not(None),
            )
            .order_by(
                ArticleAnalysis.embedding.cosine_distance(source_embedding.c.embedding)
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.unique().scalars().all())
