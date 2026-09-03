"""Agent-facing internal search service boundary."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

import structlog

from app.agent.evidence_collection.internal_search.contract import (
    INTERNAL_SEARCH_HIT_POOL_LIMIT,
    INTERNAL_SEARCH_HITS_PER_QUERY,
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


_INTERNAL_SEARCH_TIMEOUT_SECONDS = 15


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
            timeout = asyncio.timeout(_INTERNAL_SEARCH_TIMEOUT_SECONDS)
            try:
                try:
                    async with timeout:
                        cache_lookup = await self._fetch_cached_query_vectors(queries)

                        hits_by_query: dict[str, InternalQueryEmbedding] = {}
                        cache_misses: list[str] = []
                        for query in queries.queries:
                            cache_hit = cache_lookup.get(query)
                            if cache_hit is None:
                                cache_misses.append(query)
                                continue
                            hits_by_query[query] = InternalQueryEmbedding(
                                query=query,
                                vector=cache_hit,
                            )

                        new_embeddings = await self._embed_queries(tuple(cache_misses))
                        await self._store_new_query_embeddings(new_embeddings)
                        hits_by_query.update(
                            {embedding.query: embedding for embedding in new_embeddings}
                        )
                        embeddings = [
                            hits_by_query[query]
                            for query in queries.queries
                            if query in hits_by_query
                        ]
                        hits = await self._search_articles(embeddings)
                except TimeoutError as cause:
                    if not timeout.expired():
                        raise
                    raise InternalSearchError(
                        code=InternalSearchFailureCode.TIMEOUT
                    ) from cause
            except InternalSearchError as exc:
                logger.warning(
                    "internal_search_failed",
                    failure_code=exc.code.value,
                    query_count=query_count,
                )
                recording.report_outcome(InternalSearchFailed(failure_code=exc.code))
                raise
            recording.report_outcome(InternalSearchSucceeded(hit_count=len(hits)))
            return hits

    async def _embed_queries(
        self,
        uncached_queries: tuple[str, ...],
    ) -> list[InternalQueryEmbedding]:
        if not uncached_queries:
            return []
        try:
            return await self.embedder.embed_queries(
                InternalSearchQueries(queries=uncached_queries)
            )
        except AIProviderError as exc:
            raise InternalSearchError(
                code=InternalSearchFailureCode.QUERY_EMBEDDING_FAILED
            ) from exc

    async def _search_articles(
        self,
        embeddings: list[InternalQueryEmbedding],
    ) -> list[InternalArticleSearchHit]:
        if self.article_search_repository is None:
            raise RuntimeError("article_search_repository is required")

        best_by_curation_id: dict[int, InternalArticleSearchHit] = {}
        for embedding in embeddings:
            hits = await self.article_search_repository.search_by_embedding(
                embedding,
                limit=INTERNAL_SEARCH_HITS_PER_QUERY,
            )
            for hit in hits:
                current = best_by_curation_id.get(hit.article.curation_id)
                if current is None or hit.distance < current.distance:
                    best_by_curation_id[hit.article.curation_id] = hit

        return sorted(
            best_by_curation_id.values(),
            key=lambda hit: hit.distance,
        )[:INTERNAL_SEARCH_HIT_POOL_LIMIT]

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
