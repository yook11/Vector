"""Abstract base analyzer with retry engine."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

import structlog

from app.ai.analyzer.errors import (
    AnalysisError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)
from app.infra.redis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)
from app.models.article_analysis import ImpactLevel

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


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
