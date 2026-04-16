"""Abstract base analyzer with single-call API invocation."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar

import structlog

from app.analysis.errors import AnalysisDomainError
from app.models.article_analysis import ImpactLevel

logger = structlog.get_logger(__name__)


@dataclass
class AnalysisData:
    """Parsed AI response data before DB persistence."""

    title: str
    summary: str
    impact_level: ImpactLevel
    reasoning: str
    keywords: list[str] | None = None


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

    Rate limiting and retry are handled by the task layer.
    """

    MODEL: ClassVar[str]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "RPM", "RPD"):
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define ClassVar '{attr}'")

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
            AnalysisDomainError: If analysis fails.
        """
        ...

    @abc.abstractmethod
    async def _call_api(self, prompt: str) -> str:
        """Call the provider SDK. Return the raw text response.

        Must NOT catch exceptions — let them propagate to _call_once.
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Classify an SDK exception into the error hierarchy.

        Return (not raise) the appropriate AnalysisDomainError subclass.
        """
        ...

    # ── single-call invocation ──────────────────────────────────

    async def _call_once(self, prompt: str) -> str:
        """Call the provider API once and translate errors.

        No retry, no rate limiting — those are the task layer's concern.
        """
        try:
            logger.info("analyzer_api_call", model=self.model_name)
            result = await self._call_api(prompt)
            logger.info("analyzer_api_success", model=self.model_name)
            return result
        except AnalysisDomainError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc
