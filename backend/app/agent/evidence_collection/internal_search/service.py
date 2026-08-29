"""Agent-facing internal search service boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from app.agent.evidence_collection.internal_search.contract import (
    InternalArticleSearchHit,
    InternalSearchError,
    InternalSearchFailureCode,
)
from app.agent.evidence_collection.internal_search.metrics import (
    record_query_embedding_cache_outcome,
)
from app.agent.evidence_collection.internal_search.query_embedding import (
    InternalQueryEmbedder,
    InternalQueryEmbedding,
    InternalSearchQueries,
)
from app.agent.recording.internal_search import (
    InternalSearchFailed,
    InternalSearchRecorder,
    InternalSearchSucceeded,
    logfire_internal_search_recorder,
)
from app.analysis.ai_provider_errors import AIProviderError
from app.analysis.embedding.domain.value_objects import EmbeddingVector

__all__ = ["InternalSearchService"]

logger = structlog.get_logger(__name__)


class ArticleVectorSearchRepository(Protocol):
    async def search_by_embedding(
        self,
        embedding: InternalQueryEmbedding,
        *,
        limit: int,
    ) -> list[InternalArticleSearchHit]: ...


class InternalQueryEmbeddingCache(Protocol):
    async def fetch_cached(
        self,
        queries: InternalSearchQueries,
    ) -> dict[str, EmbeddingVector]: ...

    async def store(
        self,
        embedding: InternalQueryEmbedding,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class InternalSearchService:
    """Internal search port (`InternalSearch`) の実装。"""

    embedder: InternalQueryEmbedder
    article_search_repository: ArticleVectorSearchRepository | None = None
    query_embedding_cache: InternalQueryEmbeddingCache | None = None
    recorder: InternalSearchRecorder = logfire_internal_search_recorder

    async def search(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalArticleSearchHit]:
        query_count = len(queries.queries)
        async with self.recorder.record(query_count=query_count) as recording:
            try:
                hits = await self._search_articles(queries)
            except InternalSearchError as exc:
                recording.report_outcome(InternalSearchFailed(failure_code=exc.code))
                raise
            recording.report_outcome(InternalSearchSucceeded(hit_count=len(hits)))
            return hits

    async def embed_queries(
        self,
        queries: InternalSearchQueries,
    ) -> list[InternalQueryEmbedding]:
        if not queries.queries:
            return []

        cached_vectors = await self._fetch_cached_query_vectors(queries)
        missing_queries = tuple(
            query for query in queries.queries if query not in cached_vectors
        )
        new_embeddings: list[InternalQueryEmbedding] = []
        if missing_queries:
            try:
                new_embeddings = await self.embedder.embed_queries(
                    InternalSearchQueries(queries=missing_queries)
                )
            except AIProviderError as exc:
                raise InternalSearchError(
                    code=InternalSearchFailureCode.EMBEDDING_PROVIDER_FAILED
                ) from exc

        await self._store_new_query_embeddings(new_embeddings)
        embeddings_by_query = {
            query: InternalQueryEmbedding(query=query, vector=vector)
            for query, vector in cached_vectors.items()
        }
        embeddings_by_query.update(
            {embedding.query: embedding for embedding in new_embeddings}
        )
        embeddings = [
            embeddings_by_query[query]
            for query in queries.queries
            if query in embeddings_by_query
        ]
        return embeddings

    async def _search_articles(
        self,
        queries: InternalSearchQueries,
        *,
        per_query_limit: int = 5,
        limit: int = 5,
    ) -> list[InternalArticleSearchHit]:
        if limit <= 0 or per_query_limit <= 0:
            return []
        if self.article_search_repository is None:
            raise RuntimeError("article_search_repository is required")

        try:
            embeddings = await self.embed_queries(queries)
            best_by_curation_id: dict[int, InternalArticleSearchHit] = {}
            for embedding in embeddings:
                hits = await self.article_search_repository.search_by_embedding(
                    embedding,
                    limit=per_query_limit,
                )
                for hit in hits:
                    current = best_by_curation_id.get(hit.article.curation_id)
                    if current is None or hit.distance < current.distance:
                        best_by_curation_id[hit.article.curation_id] = hit

            hits = sorted(
                best_by_curation_id.values(),
                key=lambda hit: hit.distance,
            )[:limit]
        except InternalSearchError as exc:
            logger.warning(
                "internal_search_failed",
                failure_code=exc.code.value,
                query_count=len(queries.queries),
            )
            raise
        return hits

    async def _fetch_cached_query_vectors(
        self,
        queries: InternalSearchQueries,
    ) -> dict[str, EmbeddingVector]:
        if self.query_embedding_cache is None:
            return {}
        try:
            return await self.query_embedding_cache.fetch_cached(queries)
        except Exception:
            record_query_embedding_cache_outcome(
                result="lookup_failed",
                query_count=len(queries.queries),
            )
            return {}

    async def _store_new_query_embeddings(
        self,
        embeddings: list[InternalQueryEmbedding],
    ) -> None:
        if self.query_embedding_cache is None:
            return
        for embedding in embeddings:
            try:
                await self.query_embedding_cache.store(embedding)
            except Exception:
                record_query_embedding_cache_outcome(
                    result="save_failed",
                    query_count=1,
                )
