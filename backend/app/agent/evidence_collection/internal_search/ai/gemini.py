"""Gemini query embedder for internal retrieval."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Final

import structlog
from google.genai.client import AsyncClient
from google.genai.types import EmbedContentConfig
from pydantic import ValidationError

from app.agent.evidence_collection.internal_search.ai.gemini_spec import (
    GEMINI_QUERY_EMBEDDING_SPEC,
    QueryEmbeddingCallSpec,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.ai_providers.errors import (
    AIProviderError,
    AIProviderResultError,
    AIProviderResultReason,
)
from app.ai_providers.gemini.error_translator import translate_gemini_error
from app.analysis.ai_provider_exhaustion import record_ai_provider_exhausted
from app.analysis.embedding.domain.value_objects import EmbeddingVector

logger = structlog.get_logger(__name__)


class GeminiQueryEmbedder:
    """呼び出しごとにクライアントを借り、生成・終了と通信設定は呼び出し元に任せる。"""

    SPEC: Final[QueryEmbeddingCallSpec] = GEMINI_QUERY_EMBEDDING_SPEC

    def __init__(
        self,
        *,
        client_scope_factory: Callable[[], AbstractAsyncContextManager[AsyncClient]],
    ) -> None:
        self._client_scope_factory = client_scope_factory

    @property
    def model_name(self) -> str:
        return self.SPEC.model

    @property
    def dimension(self) -> int:
        return self.SPEC.dimension

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
        async with self._client_scope_factory() as client:
            try:
                logger.info(
                    "internal_query_embed_api_call",
                    model=self.model_name,
                    query_count=len(queries.queries),
                )
                vectors = await self._call_api(client, queries)
                logger.info(
                    "internal_query_embed_api_success",
                    model=self.model_name,
                    query_count=len(queries.queries),
                )
                return vectors
            except AIProviderError:
                raise
            except ValidationError as exc:
                raise AIProviderResultError(
                    reason=AIProviderResultReason.RESPONSE_UNPARSEABLE
                ) from exc
            except Exception as exc:
                translated = self._translate_error(exc)
                if translated is exc:
                    raise
                record_ai_provider_exhausted(translated, provider=self.SPEC.provider)
                raise translated from exc

    async def _call_api(
        self, client: AsyncClient, queries: InternalSearchQueries
    ) -> list[list[float]]:
        response = await client.models.embed_content(
            model=self.SPEC.model,
            contents=list(queries.queries),
            config=EmbedContentConfig(
                output_dimensionality=self.SPEC.output_dimensionality,
                task_type=self.SPEC.task_type,
            ),
        )
        embeddings = response.embeddings
        if not embeddings:
            raise AIProviderResultError(reason=AIProviderResultReason.EMBEDDINGS_EMPTY)
        if len(embeddings) != len(queries.queries):
            raise AIProviderResultError(
                reason=AIProviderResultReason.EMBEDDING_COUNT_MISMATCH
            )

        vectors: list[list[float]] = []
        for embedding in embeddings:
            if embedding.values is None:
                raise AIProviderResultError(
                    reason=AIProviderResultReason.EMBEDDING_VALUES_MISSING
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
