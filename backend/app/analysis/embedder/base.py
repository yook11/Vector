"""Abstract base embedder with single-call API invocation."""

from __future__ import annotations

import abc
from typing import ClassVar

import structlog

from app.analysis.errors import AnalysisDomainError

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

    Rate limiting and retry are handled by the task layer.
    """

    MODEL: ClassVar[str]
    DIMENSION: ClassVar[int]
    RPM: ClassVar[int | None]
    RPD: ClassVar[int | None]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if getattr(cls, "__abstractmethods__", None):
            return
        for attr in ("MODEL", "DIMENSION", "RPM", "RPD"):
            if attr not in cls.__dict__:
                raise TypeError(f"{cls.__name__} must define ClassVar '{attr}'")

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
        vectors = await self._embed_once(text, "RETRIEVAL_DOCUMENT")
        return vectors[0]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed multiple document texts in a single API call."""
        return await self._embed_once(texts, "RETRIEVAL_DOCUMENT")

    async def embed_query(self, text: str) -> list[float]:
        """Embed a search query."""
        vectors = await self._embed_once(text, "RETRIEVAL_QUERY")
        return vectors[0]

    # -- single-call invocation ------------------------------------------

    async def _embed_once(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call the provider API once and translate errors.

        No retry, no rate limiting — those are the task layer's concern.
        """
        try:
            logger.info(
                "embed_api_call",
                model=self.model_name,
                task_type=task_type,
                batch_size=len(contents) if isinstance(contents, list) else 1,
            )
            vectors = await self._call_api(contents, task_type)
            logger.info(
                "embed_api_success",
                model=self.model_name,
                count=len(vectors),
            )
            return vectors
        except AnalysisDomainError:
            raise
        except Exception as exc:
            raise self._translate_error(exc) from exc

    # -- abstract hooks (subclass provides) ------------------------------

    @abc.abstractmethod
    async def _call_api(
        self, contents: str | list[str], task_type: str
    ) -> list[list[float]]:
        """Call the provider SDK. Return a list of vectors.

        Must return ``list[list[float]]`` even for a single text.
        Must NOT catch exceptions — let them propagate to _embed_once.
        """
        ...

    @abc.abstractmethod
    def _translate_error(self, exc: Exception) -> AnalysisDomainError:
        """Classify an SDK exception into the error hierarchy.

        Return (not raise) the appropriate AnalysisDomainError subclass.
        """
        ...
