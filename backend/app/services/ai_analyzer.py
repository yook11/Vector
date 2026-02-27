"""AI analyzer service — abstract base and orchestration layer."""

import abc
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.models.analysis import AnalysisResult, AnalysisTranslation
from app.models.investment_category import (
    AnalysisInvestmentCategory,
    InvestmentCategory,
)
from app.models.news import NewsArticle

logger = structlog.get_logger(__name__)

REQUEST_INTERVAL = 1.5  # seconds between API requests (Gemini free tier RPM)


class AnalysisError(Exception):
    """Raised when AI analysis fails."""


class RateLimitError(AnalysisError):
    """Raised when AI API returns 429 (rate limit exceeded)."""


@dataclass
class AnalysisData:
    """Parsed AI response data before DB persistence."""

    title: str
    summary: str
    sentiment: str  # "positive" | "negative" | "neutral"
    impact_score: int  # 1-10
    reasoning: str | None = None
    investment_categories: list[str] | None = None


@dataclass
class AnalyzeResult:
    """Result of analyzing articles: counts of success/skip/error."""

    analyzed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


class BaseAnalyzer(abc.ABC):
    """Abstract base class for AI analyzers."""

    @abc.abstractmethod
    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,
    ) -> AnalysisData:
        """Analyze a news article and return structured analysis data.

        Args:
            title: English article title.
            description: English article description/summary (may be None).
            content: Full article text (may be None).

        Returns:
            AnalysisData with Japanese translation, sentiment, and score.

        Raises:
            AnalysisError: If analysis fails after retries.
        """
        ...

    @property
    @abc.abstractmethod
    def provider_name(self) -> str:
        """Return the provider identifier (e.g., 'gemini')."""
        ...

    @property
    @abc.abstractmethod
    def model_name(self) -> str:
        """Return the model identifier (e.g., 'gemini-2.0-flash')."""
        ...


def get_analyzer() -> BaseAnalyzer:
    """Factory: return an analyzer instance based on settings.ai_provider.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.services.gemini_analyzer import GeminiAnalyzer

        return GeminiAnalyzer()
    raise ValueError(f"Unsupported AI provider: {provider}")


async def analyze_article(
    session: AsyncSession,
    article: NewsArticle,
    analyzer: BaseAnalyzer | None = None,
) -> AnalysisResult | None:
    """Analyze a single news article and persist the result.

    Returns the created AnalysisResult, or None if already analyzed.

    Raises:
        AnalysisError: If the AI provider fails. Callers using this
            function directly (outside analyze_articles) must handle
            this exception.
    """
    # Explicit query to check if already analyzed (avoids MissingGreenlet)
    stmt = select(AnalysisResult).where(AnalysisResult.news_article_id == article.id)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "analysis_skipped",
            article_id=article.id,
            reason="already analyzed",
        )
        return None

    if analyzer is None:
        analyzer = get_analyzer()

    try:
        data = await analyzer.analyze(
            title=article.title_original,
            description=article.description_original,
            content=article.content,
        )
    except AnalysisError as e:
        logger.error("analysis_failed", article_id=article.id, error=str(e))
        raise

    result = AnalysisResult(
        news_article_id=article.id,
        sentiment=data.sentiment,
        impact_score=data.impact_score,
        reasoning=data.reasoning,
        ai_provider=analyzer.provider_name,
        ai_model=analyzer.model_name,
        analyzed_at=datetime.now(UTC),
    )
    session.add(result)
    await session.flush()

    # Persist translation
    translation = AnalysisTranslation(
        analysis_id=result.id,
        locale="ja",
        title=data.title,
        summary=data.summary,
    )
    session.add(translation)

    # Persist investment category links
    if data.investment_categories:
        cat_stmt = select(InvestmentCategory).where(
            InvestmentCategory.slug.in_(data.investment_categories)
        )
        categories = (await session.execute(cat_stmt)).scalars().all()
        for cat in categories:
            link = AnalysisInvestmentCategory(
                analysis_id=result.id,
                category_id=cat.id,
            )
            session.add(link)

    logger.info(
        "analysis_completed",
        article_id=article.id,
        sentiment=data.sentiment,
        impact_score=data.impact_score,
        categories=data.investment_categories,
    )
    return result


async def analyze_articles(
    session: AsyncSession,
    articles: list[NewsArticle],
    analyzer: BaseAnalyzer | None = None,
) -> AnalyzeResult:
    """Analyze multiple articles sequentially with rate limit protection.

    AnalysisError from individual articles is caught and accumulated
    in AnalyzeResult.errors so that the batch continues processing.
    """
    result = AnalyzeResult()

    if not articles:
        logger.info("analyze_batch_skipped", reason="no articles provided")
        return result

    if analyzer is None:
        analyzer = get_analyzer()

    # Detach articles so rollback/commit won't expire their attributes.
    # rollback() expires ALL objects in the session (expire_on_commit=False
    # only protects against commit), causing MissingGreenlet on next access.
    for a in articles:
        session.expunge(a)

    for i, article in enumerate(articles):
        # Rate limit: sleep between API requests (skip before first)
        if i > 0:
            await asyncio.sleep(REQUEST_INTERVAL)

        article_id = article.id

        try:
            analysis = await analyze_article(session, article, analyzer)
            if analysis is None:
                result.skipped_count += 1
            else:
                await session.commit()
                result.analyzed_count += 1
                logger.info("article_saved", article_id=article_id)
        except RateLimitError as e:
            await session.rollback()
            result.error_count += 1
            result.errors.append(f"Article {article_id}: {e}")
            logger.warning(
                "analyze_batch_rate_limited",
                article_id=article_id,
                remaining=len(articles) - i - 1,
            )
            break
        except AnalysisError as e:
            await session.rollback()
            result.error_count += 1
            result.errors.append(f"Article {article_id}: {e}")
            continue
        except Exception as e:
            await session.rollback()
            result.error_count += 1
            result.errors.append(f"Article {article_id}: {e}")
            logger.error(
                "article_analysis_unexpected_error",
                article_id=article_id,
                error=str(e),
            )
            continue

    logger.info(
        "analyze_batch_completed",
        analyzed=result.analyzed_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
