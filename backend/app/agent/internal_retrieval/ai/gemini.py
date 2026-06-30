"""Gemini query embedder for internal retrieval."""

from __future__ import annotations

from typing import Final

import structlog
from google import genai
from google.genai.types import EmbedContentConfig
from pydantic import ValidationError

from app.agent.internal_retrieval.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
    QueryEmbeddingCallSpec,
)
from app.agent.internal_retrieval.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.analysis.ai_provider_errors import (
    AIProviderConfigurationError,
    AIProviderError,
    AIProviderRequestInvalidError,
)
from app.analysis.embedding.domain.value_objects import EmbeddingVector
from app.analysis.gemini_error_translator import (
    GeminiStateReason,
    translate_gemini_error,
)
from app.analysis.rate_limit import AIModelRateLimitPolicy
from app.config import settings

logger = structlog.get_logger(__name__)


class GeminiQueryEmbedder:
    """Gemini implementation of the internal query embedder protocol."""

    SPEC: Final[QueryEmbeddingCallSpec] = GEMINI_QUERY_EMBEDDING_SPEC

    def __init__(self) -> None:
        api_key = settings.gemini_api_key.get_secret_value()
        if not api_key:
            raise AIProviderConfigurationError(reason=GeminiStateReason.NOT_CONFIGURED)
        self._client = genai.Client(api_key=api_key)

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def dimension(self) -> int:
        return self.SPEC.dimension

    @property
    def rate_limit_policy(self) -> AIModelRateLimitPolicy:
        return self.SPEC.rate_limit_policy

    async def embed_queries(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalQueryEmbedding]:
        if not queries.queries:
            return []

        raw_vectors = await self._embed_once(queries)
        return [
            self._to_query_embedding(query, raw_vector)
            for query, raw_vector in zip(queries.queries, raw_vectors, strict=True)
        ]

    async def _embed_once(self, queries: InternalSearchQueries) -> list[list[float]]:
        try:
            logger.info(
                "internal_query_embed_api_call",
                model=self.model_name,
                query_count=len(queries.queries),
            )
            vectors = await self._call_api(queries)
            logger.info(
                "internal_query_embed_api_success",
                model=self.model_name,
                query_count=len(queries.queries),
            )
            return vectors
        except AIProviderError:
            raise
        except ValidationError as exc:
            raise AIProviderRequestInvalidError(
                reason=GeminiStateReason.INVALID_ARGUMENT
            ) from exc
        except Exception as exc:
            translated = self._translate_error(exc)
            if translated is exc:
                raise
            raise translated from exc

    async def _call_api(self, queries: InternalSearchQueries) -> list[list[float]]:
        response = await self._client.aio.models.embed_content(
            model=self.SPEC.model,
            contents=list(queries.queries),
            config=EmbedContentConfig(
                output_dimensionality=self.SPEC.output_dimensionality,
                task_type=self.SPEC.task_type,
            ),
        )
        embeddings = response.embeddings
        if not embeddings:
            raise AIProviderRequestInvalidError(
                reason=GeminiStateReason.EMPTY_EMBEDDINGS
            )
        if len(embeddings) != len(queries.queries):
            raise AIProviderRequestInvalidError(
                reason=GeminiStateReason.EMBEDDING_COUNT_MISMATCH
            )

        vectors: list[list[float]] = []
        for embedding in embeddings:
            if embedding.values is None:
                raise AIProviderRequestInvalidError(
                    reason=GeminiStateReason.MISSING_VALUES
                )
            vectors.append(list(embedding.values))
        return vectors

    @staticmethod
    def _to_query_embedding(
        query: str,
        raw_vector: list[float],
    ) -> InternalQueryEmbedding:
        vector = EmbeddingVector(root=tuple(raw_vector))
        return InternalQueryEmbedding(query=query, vector=vector)

    def _translate_error(self, exc: Exception) -> Exception:
        return translate_gemini_error(exc)
