"""Agent-facing internal search service boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.agent.contract import QuestionPlan
from app.agent.internal_retrieval.article_search import InternalArticleSearchHit
from app.agent.internal_retrieval.metrics import (
    record_internal_retrieval_outcome,
    record_query_embedding_cache_outcome,
)
from app.agent.internal_retrieval.query_embedding import (
    InternalQueryEmbedder,
    InternalQueryEmbedding,
    InternalSearchQueries,
    build_internal_search_queries,
)
from app.analysis.embedding.domain.value_objects import EmbeddingVector

__all__ = ["InternalSearchService"]


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
    """Agent-facing internal search entrypoint for this query-embedding slice."""

    embedder: InternalQueryEmbedder
    article_search_repository: ArticleVectorSearchRepository | None = None
    query_embedding_cache: InternalQueryEmbeddingCache | None = None

    async def embed_plan_queries(
        self,
        plan: QuestionPlan,
    ) -> list[InternalQueryEmbedding]:
        if plan.retrieval_mode not in {"internal", "internal_and_external"}:
            return []

        queries = build_internal_search_queries(plan.internal_queries)
        if not queries.queries:
            return []

        cached_vectors = await self._fetch_cached_query_vectors(queries)
        missing_queries = tuple(
            query for query in queries.queries if query not in cached_vectors
        )
        new_embeddings: list[InternalQueryEmbedding] = []
        try:
            if missing_queries:
                new_embeddings = await self.embedder.embed_queries(
                    InternalSearchQueries(queries=missing_queries)
                )
        except Exception:
            record_internal_retrieval_outcome(
                result="failed",
                query_count=len(queries.queries),
            )
            raise

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
        record_internal_retrieval_outcome(
            result="succeeded" if embeddings else "empty",
            query_count=len(queries.queries),
        )
        return embeddings

    async def search_plan_articles(
        self,
        plan: QuestionPlan,
        *,
        per_query_limit: int = 5,
        limit: int = 5,
    ) -> list[InternalArticleSearchHit]:
        if limit <= 0 or per_query_limit <= 0:
            return []
        if self.article_search_repository is None:
            raise RuntimeError("article_search_repository is required")

        embeddings = await self.embed_plan_queries(plan)
        if not embeddings:
            return []

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

        return sorted(
            best_by_curation_id.values(),
            key=lambda hit: hit.distance,
        )[:limit]

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
