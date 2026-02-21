"""AI analyzer service — abstract base and orchestration layer."""

import abc
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.models.analysis import AnalysisResult
from app.models.news import NewsArticle

logger = structlog.get_logger(__name__)

REQUEST_INTERVAL = 1.0  # seconds between API requests (rate limit protection)


class AnalysisError(Exception):
    """Raised when AI analysis fails."""


@dataclass
class AnalysisData:
    """Parsed AI response data before DB persistence."""

    title_ja: str
    summary_ja: str
    sentiment: str  # "positive" | "negative" | "neutral"
    impact_score: int  # 1-10
    key_topics: list[str] | None = None
    reasoning: str | None = None


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
    stmt = select(AnalysisResult).where(
        AnalysisResult.news_article_id == article.id
    )
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
        title_ja=data.title_ja,
        summary_ja=data.summary_ja,
        sentiment=data.sentiment,
        impact_score=data.impact_score,
        key_topics=data.key_topics,
        reasoning=data.reasoning,
        ai_provider=analyzer.provider_name,
        ai_model=analyzer.model_name,
        analyzed_at=datetime.now(UTC),
    )
    session.add(result)
    await session.flush()

    logger.info(
        "analysis_completed",
        article_id=article.id,
        sentiment=data.sentiment,
        impact_score=data.impact_score,
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

    for i, article in enumerate(articles):
        # Rate limit: sleep between API requests (skip before first)
        if i > 0:
            await asyncio.sleep(REQUEST_INTERVAL)

        try:
            analysis = await analyze_article(session, article, analyzer)
            if analysis is None:
                result.skipped_count += 1
            else:
                result.analyzed_count += 1
        except AnalysisError as e:
            result.error_count += 1
            result.errors.append(f"Article {article.id}: {e}")

    await session.commit()

    logger.info(
        "analyze_batch_completed",
        analyzed=result.analyzed_count,
        skipped=result.skipped_count,
        errors=result.error_count,
    )
    return result
