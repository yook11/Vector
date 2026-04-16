"""Analysis repository — DB operations for the analysis domain."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.article_analysis import ArticleAnalysis
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle


class AnalysisRepository:
    """Encapsulates SQL operations for article analysis and embedding."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_article_id(self, article_id: int) -> ArticleAnalysis | None:
        """Find existing analysis for idempotency check."""
        stmt = select(ArticleAnalysis).where(
            ArticleAnalysis.news_article_id == article_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_article(self, article_id: int) -> NewsArticle | None:
        """Fetch a news article by ID."""
        return await self._session.get(NewsArticle, article_id)

    async def get_keywords_by_category(self) -> dict[str, list[str]] | None:
        """Fetch all keyword candidates grouped by category slug."""
        stmt = select(Category.slug, Keyword.name).join(
            Keyword,
            Keyword.category_id == Category.id,
        )
        rows = (await self._session.execute(stmt)).all()
        if not rows:
            return None
        result: dict[str, list[str]] = {}
        for slug, kw in rows:
            result.setdefault(str(slug), []).append(str(kw))
        return result

    async def save_analysis(
        self,
        analysis: ArticleAnalysis,
        keyword_names: list[str] | None,
    ) -> ArticleAnalysis:
        """Persist analysis result and keyword links (flush, no commit)."""
        self._session.add(analysis)
        await self._session.flush()

        if keyword_names:
            stmt = select(Keyword).where(Keyword.name.in_(keyword_names))
            matched = (await self._session.execute(stmt)).scalars().all()
            for kw in matched:
                link = ArticleKeyword(
                    article_analysis_id=analysis.id,
                    keyword_id=kw.id,
                )
                self._session.add(link)

        return analysis

    async def save_embedding(
        self,
        analysis: ArticleAnalysis,
        vector: list[float],
        model: str,
    ) -> None:
        """Persist embedding result on an existing analysis."""
        analysis.embedding = vector
        analysis.embedding_model = model
        self._session.add(analysis)

    async def mark_article_skipped(self, article: NewsArticle) -> None:
        """Mark an article for permanent skip."""
        article.original_content = None
        article.skip_content_fetch = True
        self._session.add(article)
