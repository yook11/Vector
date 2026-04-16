"""Analysis service — orchestration and DB persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.analysis.analyzer.base import BaseAnalyzer
from app.analysis.errors import InvalidInputError
from app.analysis.repository import AnalysisRepository
from app.models.article_analysis import ArticleAnalysis
from app.utils.sanitize import strip_html_tags

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AnalysisResult:
    """Result of article analysis use case."""

    status: Literal["created", "already_exists", "skipped"]
    analysis_id: int | None = None


class ArticleAnalysisService:
    """Atomic use case: analyze a single article and persist the result.

    Session management is internal — callers provide only a session factory.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def execute(self, article_id: int, analyzer: BaseAnalyzer) -> AnalysisResult:
        """Run analysis for a single article.

        Returns:
            AnalysisResult with status and optional analysis_id.

        Raises:
            AnalysisDomainError subclasses (except InvalidInputError) — caller
            must handle retry decisions.
        """
        async with self._session_factory() as session:
            repo = AnalysisRepository(session)

            # Idempotency check
            existing = await repo.find_by_article_id(article_id)
            if existing is not None:
                return AnalysisResult("already_exists", analysis_id=existing.id)

            # Fetch article
            article = await repo.get_article(article_id)
            if article is None:
                logger.warning("analysis_article_not_found", article_id=article_id)
                return AnalysisResult("skipped")

            # Fetch keyword candidates
            keywords_by_category = await repo.get_keywords_by_category()

            # AI analysis
            try:
                data = await analyzer.analyze(
                    title=article.original_title,
                    description=article.original_description,
                    content=article.original_content,
                    keywords_by_category=keywords_by_category,
                )
            except InvalidInputError:
                await repo.mark_article_skipped(article)
                await session.commit()
                logger.warning(
                    "analysis_invalid_input",
                    article_id=article_id,
                )
                return AnalysisResult("skipped")

            # Sanitize & persist
            analysis = ArticleAnalysis(
                news_article_id=article.id,
                translated_title=strip_html_tags(data.title) or "",
                summary=strip_html_tags(data.summary) or "",
                impact_level=data.impact_level,
                reasoning=strip_html_tags(data.reasoning) or "",
                ai_model=analyzer.model_name,
            )
            await repo.save_analysis(analysis, data.keywords)
            await session.commit()

            logger.info(
                "analysis_completed",
                article_id=article_id,
                impact_level=data.impact_level,
                keywords=data.keywords,
            )
            return AnalysisResult("created", analysis_id=analysis.id)


async def mark_article_skipped(
    session_factory: async_sessionmaker[AsyncSession],
    article_id: int,
) -> None:
    """Mark an article for permanent skip (for Task last-attempt use)."""
    async with session_factory() as session:
        repo = AnalysisRepository(session)
        article = await repo.get_article(article_id)
        if article is not None:
            await repo.mark_article_skipped(article)
            await session.commit()
