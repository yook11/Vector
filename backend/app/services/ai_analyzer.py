"""AI analyzer service — abstract base and orchestration layer."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.infra.redis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.models.article_analysis import ArticleAnalysis, ImpactLevel
from app.models.article_keyword import ArticleKeyword
from app.models.category import Category
from app.models.keyword import Keyword
from app.models.news_article import NewsArticle
from app.utils.sanitize import strip_html_tags

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


class AnalysisError(Exception):
    """Raised when AI analysis fails (base / unclassifiable)."""


class RateLimitError(AnalysisError):
    """HTTP 429 — rate limit exceeded."""


class TransientError(AnalysisError):
    """5xx, network errors, timeouts — expected to recover with time."""


class InvalidInputError(AnalysisError):
    """4xx (except 429) — input-caused, permanent. Retry is pointless."""


class DailyQuotaExhaustedError(AnalysisError):
    """RPD limit reached — no more requests allowed today."""


@dataclass
class AnalysisData:
    """Parsed AI response data before DB persistence."""

    title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    keywords: list[str] | None = None


@dataclass
class AnalyzeResult:
    """Result of analyzing articles: counts of success/skip/error."""

    analyzed_count: int = 0
    skipped_count: int = 0
    error_count: int = 0
    errors: list[str] = field(default_factory=list)


class BaseAnalyzer(abc.ABC):
    """Template Method base for AI analyzers.

    Subclasses implement three hooks:
    - ``analyze``: prompt building + response parsing (public API)
    - ``_call_api``: raw SDK call (no error handling)
    - ``_translate_error``: classify SDK exceptions into the error hierarchy

    Subclasses must declare these ClassVars:
    - ``MODEL``: model identifier (e.g. ``"gemini-2.5-flash-lite"``)
    - ``RPM``: requests-per-minute limit, or ``None`` if unlimited
    - ``RPD``: requests-per-day limit, or ``None`` if unlimited

    Retry logic, delay strategy, and error-handling policy live here.
    """

    MODEL: ClassVar[str]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

    MAX_RETRIES = 3
    RETRY_BASE_DELAY = 2.0  # exponential backoff: 2, 4, 8
    RATE_LIMIT_DELAY = 10.0
    MAX_RATE_LIMIT_RETRIES = 1

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "RPM", "RPD"):
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define ClassVar '{attr}'")

    def __init__(
        self,
        *,
        rpm_limiter: RateLimiter | None = None,
        rpd_limiter: RateLimiter | None = None,
    ) -> None:
        self._rpm_limiter = rpm_limiter
        self._rpd_limiter = rpd_limiter

    @property
    def model_name(self) -> str:
        """Model identifier (e.g., 'gemini-2.5-flash-lite')."""
        return self.MODEL

    # ── abstract hooks (subclass provides) ──────────────────────

    @abc.abstractmethod
    async def analyze(
        self,
        title: str,
        description: str | None,
        content: str | None = None,
        keywords_by_category: dict[str, list[str]] | None = None,
    ) -> AnalysisData:
        """Analyze a news article and return structured analysis data.

        Args:
            title: English article title.
            description: English article description/summary (may be None).
            content: Full article text (may be None).
            keywords_by_category: Optional dict mapping category slug to keyword
                names. AI selects the most relevant keywords across all categories.

        Returns:
            AnalysisData with Japanese translation, impact level, and reasoning.

        Raises:
            AnalysisError: If analysis fails after retries.
        """
        ...

    @abc.abstractmethod
    async def _call_api(self, prompt: str) -> str:
        """Call the provider SDK. Return the raw text response.

        Must NOT catch exceptions — let them propagate to _call_with_retry.
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisError:
        """Classify an SDK exception into the error hierarchy.

        Return (not raise) the appropriate AnalysisError subclass.
        """
        ...

    # ── retry engine ────────────────────────────────────────────

    async def _call_with_retry(self, prompt: str) -> str:
        """Call the provider API with two-tier retry.

        - RateLimitError: fixed delay, independent budget (max 1)
        - TransientError: exponential backoff (max MAX_RETRIES)
        - InvalidInputError / AnalysisError: immediate raise
        """
        last_error: Exception | None = None
        attempt = 0
        rate_limit_retries = 0

        while attempt < self.MAX_RETRIES:
            attempt += 1
            try:
                # Acquire rate-limit slots (RPD first — fail fast)
                if self._rpd_limiter is not None:
                    await self._rpd_limiter.acquire()
                if self._rpm_limiter is not None:
                    await self._rpm_limiter.acquire()

                logger.info(
                    "analyzer_api_call",
                    model=self.model_name,
                    attempt=attempt,
                )
                result = await self._call_api(prompt)
                logger.info(
                    "analyzer_api_success",
                    model=self.model_name,
                    attempt=attempt,
                )
                return result

            except AnalysisError:
                raise
            except _RateLimitExceededError as exc:
                raise DailyQuotaExhaustedError(str(exc)) from exc
            except Exception as exc:
                last_error = exc
                error = self._translate_error(exc)

                if isinstance(error, RateLimitError):
                    rate_limit_retries += 1
                    logger.warning(
                        "analyzer_rate_limited",
                        model=self.model_name,
                        attempt=attempt,
                        rate_limit_retry=rate_limit_retries,
                        delay_seconds=self.RATE_LIMIT_DELAY,
                        error=str(exc),
                    )
                    if rate_limit_retries <= self.MAX_RATE_LIMIT_RETRIES:
                        await asyncio.sleep(self.RATE_LIMIT_DELAY)
                        attempt -= 1  # don't consume normal retry budget
                        continue
                    raise error from exc

                if isinstance(error, InvalidInputError):
                    raise error from exc

                if isinstance(error, TransientError):
                    logger.warning(
                        "analyzer_transient_error",
                        model=self.model_name,
                        attempt=attempt,
                        max_retries=self.MAX_RETRIES,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    if attempt < self.MAX_RETRIES:
                        delay = self.RETRY_BASE_DELAY * (2 ** (attempt - 1))
                        await asyncio.sleep(delay)
                    continue

                # Unknown error — don't retry
                raise error from exc

        raise AnalysisError(
            f"Analysis failed after {self.MAX_RETRIES} attempts: {last_error}"
        )


def _build_limiters(
    analyzer_cls: type[BaseAnalyzer],
) -> dict[str, RateLimiter | None]:
    """Read ClassVars and build RateLimiter instances."""
    from app.infra.redis.cache import _get_client
    from app.infra.redis.rate_limiter import RateLimiter

    redis = _get_client()
    rpm_limiter: RateLimiter | None = None
    rpd_limiter: RateLimiter | None = None

    if analyzer_cls.RPM is not None:
        rpm_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{analyzer_cls.MODEL}:rpm",
            max_requests=analyzer_cls.RPM,
            window_seconds=60,
            block=True,
        )
    if analyzer_cls.RPD is not None:
        rpd_limiter = RateLimiter(
            redis=redis,
            key=f"ratelimit:{analyzer_cls.MODEL}:rpd",
            max_requests=analyzer_cls.RPD,
            window_seconds=86400,
            block=False,
        )
    return {"rpm_limiter": rpm_limiter, "rpd_limiter": rpd_limiter}


def get_analyzer() -> BaseAnalyzer:
    """Factory: return an analyzer instance based on settings.ai_provider.

    Reads ClassVars (RPM, RPD) from the analyzer class and builds
    RateLimiter instances to inject via the constructor.

    Raises:
        ValueError: If ai_provider is not supported.
    """
    provider = settings.ai_provider.lower()
    if provider == "gemini":
        from app.services.gemini_analyzer import GeminiAnalyzer

        limiters = _build_limiters(GeminiAnalyzer)
        return GeminiAnalyzer(**limiters)
    raise ValueError(f"Unsupported AI provider: {provider}")


async def analyze_article(
    session: AsyncSession,
    article: NewsArticle,
    analyzer: BaseAnalyzer | None = None,
) -> ArticleAnalysis | None:
    """Analyze a single news article and persist the result.

    Returns the created ArticleAnalysis, or None if already analyzed.

    Raises:
        AnalysisError: If the AI provider fails. Callers using this
            function directly (outside analyze_articles) must handle
            this exception.
    """
    if analyzer is None:
        analyzer = get_analyzer()

    # Check if already analyzed (1:1 relationship)
    stmt = select(ArticleAnalysis).where(
        ArticleAnalysis.news_article_id == article.id,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "analysis_skipped",
            article_id=article.id,
            reason="already analyzed",
        )
        return None

    # Query all keyword candidates grouped by category
    keywords_by_category: dict[str, list[str]] | None = None
    kw_stmt = select(Category.slug, Keyword.name).join(
        Keyword, Keyword.category_id == Category.id
    )
    rows = (await session.execute(kw_stmt)).all()
    if rows:
        kw_dict: dict[str, list[str]] = {}
        for slug, kw in rows:
            kw_dict.setdefault(str(slug), []).append(str(kw))
        keywords_by_category = kw_dict

    try:
        data = await analyzer.analyze(
            title=article.original_title,
            description=article.original_description,
            content=article.original_content,
            keywords_by_category=keywords_by_category,
        )
    except AnalysisError as e:
        logger.error("analysis_failed", article_id=article.id, error=str(e))
        raise

    # --- XSS対策: 多層防御 (Defense in Depth) ---
    # AI レスポンスは外部入力と同等に扱い、DB 永続化の直前でサニタイズする。
    analysis = ArticleAnalysis(
        news_article_id=article.id,
        translated_title=strip_html_tags(data.title) or "",
        summary=strip_html_tags(data.summary) or "",
        impact_level=data.impact_level,
        reasoning=strip_html_tags(data.reasoning) or "",
        ai_model=analyzer.model_name,
    )
    session.add(analysis)
    await session.flush()

    # Persist keyword links (AI-selected tags from keyword_candidates)
    if data.keywords:
        kw_stmt = select(Keyword).where(Keyword.name.in_(data.keywords))
        matched_kws = (await session.execute(kw_stmt)).scalars().all()
        for kw in matched_kws:
            link = ArticleKeyword(
                article_analysis_id=analysis.id,
                keyword_id=kw.id,
            )
            session.add(link)

    logger.info(
        "analysis_completed",
        article_id=article.id,
        impact_level=data.impact_level,
        keywords=data.keywords,
    )
    return analysis


async def analyze_articles(
    session: AsyncSession,
    articles: list[NewsArticle],
    analyzer: BaseAnalyzer | None = None,
) -> AnalyzeResult:
    """Analyze multiple articles sequentially.

    Rate limiting is handled by the analyzer's RateLimiter instances
    (acquired inside ``_call_with_retry``).  This function only manages
    iteration, DB persistence, and error accumulation.

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
    for a in articles:
        session.expunge(a)

    for i, article in enumerate(articles):
        article_id = article.id

        try:
            analysis = await analyze_article(session, article, analyzer)
            if analysis is None:
                result.skipped_count += 1
            else:
                await session.commit()
                result.analyzed_count += 1
                logger.info("article_saved", article_id=article_id)
        except DailyQuotaExhaustedError as e:
            await session.rollback()
            remaining = len(articles) - i
            result.error_count += remaining
            result.errors.append(str(e))
            logger.warning(
                "analyze_daily_quota_exhausted",
                article_id=article_id,
                remaining=remaining,
                error=str(e),
            )
            break
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
