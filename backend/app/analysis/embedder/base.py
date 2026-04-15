"""Abstract base embedder with retry engine."""

from __future__ import annotations

import abc
import asyncio
from typing import TYPE_CHECKING, ClassVar

import structlog

from app.analysis.errors import (
    AnalysisDomainError,
    DailyQuotaExhaustedError,
    InvalidInputError,
    RateLimitError,
    TransientError,
)
from app.infra.redis.rate_limiter import (
    RateLimitExceededError as _RateLimitExceededError,
)

if TYPE_CHECKING:
    from app.infra.redis.rate_limiter import RateLimiter

logger = structlog.get_logger(__name__)


class BaseEmbedder(abc.ABC):
    """Template Method base for text embedders.

    Subclasses implement two hooks:
    - ``_call_api``: raw SDK call (no error handling)
    - ``_translate_error``: classify SDK exceptions into the error hierarchy

    Subclasses must declare these ClassVars:
    - ``MODEL``: model identifier (e.g. ``"gemini-embedding-001"``)
    - ``DIMENSION``: output vector dimension (e.g. ``768``)
    - ``RPM``: requests-per-minute limit, or ``None`` if unlimited
    - ``RPD``: requests-per-day limit, or ``None`` if unlimited

    Retry logic, delay strategy, and error-handling policy live here.
    """

    MODEL: ClassVar[str]
    DIMENSION: ClassVar[int]
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
        for attr in ("MODEL", "DIMENSION", "RPM", "RPD"):
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
    def dimension(self) -> int:
        """Output vector dimension (e.g., 768)."""
        return self.DIMENSION

    @property
    def model_name(self) -> str:
        """Model identifier (e.g., 'gemini-embedding-001')."""
        return self.MODEL

    # -- public API (concrete) -------------------------------------------

    async def embed_document(self, text: str) -> list[float]:
        """Embed a single document text."""
        vectors = await self._embed_with_retry(text, "RETRIEVAL_DOCUMENT")
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple document texts in a single API call."""
        return await self._embed_with_retry(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query."""
        vectors = await self._embed_with_retry(text, "RETRIEVAL_QUERY")
        return vectors[0]

    # -- retry engine ----------------------------------------------------

    async def _embed_with_retry(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call the provider API with two-tier retry.

        - RateLimitError: fixed delay, independent budget (max 1)
        - TransientError: exponential backoff (max MAX_RETRIES)
        - InvalidInputError / AnalysisDomainError: immediate raise
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
                    "embed_api_call",
                    model=self.model_name,
                    attempt=attempt,
                    task_type=task_type,
                    batch_size=len(contents) if isinstance(contents, list) else 1,
                )
                vectors = await self._call_api(contents, task_type)
                logger.info(
                    "embed_api_success",
                    model=self.model_name,
                    attempt=attempt,
                    count=len(vectors),
                )
                return vectors

            except AnalysisDomainError:
                raise
            except _RateLimitExceededError as exc:
                raise DailyQuotaExhaustedError(str(exc)) from exc
            except Exception as exc:
                last_error = exc
                error = self._translate_error(exc)

                if isinstance(error, RateLimitError):
                    rate_limit_retries += 1
                    logger.warning(
                        "embed_rate_limited",
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
                        "embed_transient_error",
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

        raise AnalysisDomainError(
            f"Embedding failed after {self.MAX_RETRIES} attempts: {last_error}"
        )

    # -- abstract hooks (subclass provides) ------------------------------

    @abc.abstractmethod
    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call the provider SDK. Return a list of vectors.

        Must return ``list[list[float]]`` even for a single text.
        Must NOT catch exceptions — let them propagate to _embed_with_retry.
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Classify an SDK exception into the error hierarchy.

        Return (not raise) the appropriate AnalysisDomainError subclass.
        """
        ...
